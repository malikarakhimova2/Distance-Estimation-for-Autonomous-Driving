"""
# -*- coding: utf-8 -*-
-----------------------------------------------------------------------------------
# Author: Nguyen Mau Dung
# DoC: 2020.08.17
# email: nguyenmaudung93.kstn@gmail.com
-----------------------------------------------------------------------------------
# Description: This script for the KITTI dataset
"""

import sys
import os
import math
from builtins import int

import numpy as np
from torch.utils.data import Dataset
import cv2
import torch

src_dir = os.path.dirname(os.path.realpath(__file__))
while not src_dir.endswith("sfa"):
    src_dir = os.path.dirname(src_dir)
if src_dir not in sys.path:
    sys.path.append(src_dir)

from data_process.kitti_data_utils import gen_hm_radius, compute_radius, Calibration, get_filtered_lidar
from data_process.kitti_bev_utils import makeBEVMap, drawRotatedBox, get_corners
from data_process import transformation
import config.kitti_config as cnf


class KittiDataset(Dataset):
    def __init__(self, configs, mode='train', lidar_aug=None, hflip_prob=None, num_samples=None):
        self.dataset_dir = configs.dataset_dir
        self.input_size = configs.input_size
        self.hm_size = configs.hm_size

        self.num_classes = configs.num_classes
        self.max_objects = configs.max_objects

        assert mode in ['train', 'val', 'test'], 'Invalid mode: {}'.format(mode)
        self.mode = mode
        self.is_test = (self.mode == 'test')
        sub_folder = 'testing' if self.is_test else 'training'

        self.lidar_aug = lidar_aug
        self.hflip_prob = hflip_prob

        self.image_dir = os.path.join(self.dataset_dir, sub_folder, "image_2")
        self.lidar_dir = os.path.join(self.dataset_dir, sub_folder, "velodyne")
        self.calib_dir = os.path.join(self.dataset_dir, sub_folder, "calib")
        self.label_dir = os.path.join(self.dataset_dir, sub_folder, "label_2")
        split_txt_path = os.path.join(self.dataset_dir, 'ImageSets', '{}.txt'.format(mode))
        self.sample_id_list = [int(x.strip()) for x in open(split_txt_path).readlines()]
        with open(split_txt_path) as f:
            raw_ids = [line.strip() for line in f.readlines()]

        self.sample_id_list = []
        for sample_id in raw_ids:
            image_path = os.path.join(self.image_dir, f"{sample_id}.png")
            lidar_path = os.path.join(self.lidar_dir, f"{sample_id}.bin")
            if os.path.isfile(image_path) and os.path.isfile(lidar_path):
                self.sample_id_list.append(sample_id)
            else:
                print(f"[SKIP] Sample {sample_id} missing: " +
                      ("image, " if not os.path.isfile(image_path) else "") +
                      ("lidar" if not os.path.isfile(lidar_path) else ""))
                
            if len(self.sample_id_list) == 0:
                raise RuntimeError("No valid samples found in the dataset! Check paths and file availability.")
            
        valid_ids = []
        for sample_id in raw_ids:
            sid = f"{int(sample_id):06d}"

            lidar_file = os.path.join(self.lidar_dir, sid + '.bin')
            image_file = os.path.join(self.image_dir, sid + '.png')
            calib_file = os.path.join(self.calib_dir, sid + '.txt')
            label_file = os.path.join(self.label_dir, sid + '.txt') if not self.is_test else None

            missing = []
            if not os.path.exists(lidar_file): missing.append('lidar')
            if not os.path.exists(image_file): missing.append('image')
            if not os.path.exists(calib_file): missing.append('calib')
            if label_file and not os.path.exists(label_file): missing.append('label')

            if missing:
                print(f"[SKIP] Sample {sid} missing: {', '.join(missing)}")
                continue

            valid_ids.append(sample_id)

            if num_samples is not None:
                valid_ids = valid_ids[:num_samples]

            self.sample_id_list = valid_ids
            self.num_samples = len(valid_ids)

        if num_samples is not None:
            self.sample_id_list = self.sample_id_list[:num_samples]
        self.num_samples = len(self.sample_id_list)

    def __len__(self):
        return len(self.sample_id_list)

    def __getitem__(self, index):
        if self.is_test:
            return self.load_img_only(index)
        else:
            return self.load_img_with_targets(index)

    def load_img_only(self, index):
        """Load only image for the testing phase"""
        sample_id = int(self.sample_id_list[index])
        img_path, img_rgb = self.get_image(sample_id)
        lidarData = self.get_lidar(sample_id)
        lidarData = get_filtered_lidar(lidarData, cnf.boundary)
        bev_map = makeBEVMap(lidarData, cnf.boundary)
        bev_map = torch.from_numpy(bev_map)

        metadatas = {
            'img_path': img_path,
        }

        return metadatas, bev_map, img_rgb

    def load_img_with_targets(self, index):
        """Load images and targets for the training and validation phase"""
        sample_id = int(self.sample_id_list[index])
        img_path = os.path.join(self.image_dir, '{:06d}.png'.format(sample_id))
        lidarData = self.get_lidar(sample_id)
        calib = self.get_calib(sample_id)
        labels, distances, has_labels = self.get_label(sample_id)
        if has_labels:
            labels[:, 1:] = transformation.camera_to_lidar_box(labels[:, 1:], calib.V2C, calib.R0, calib.P2)

        if self.lidar_aug:
            lidarData, labels[:, 1:] = self.lidar_aug(lidarData, labels[:, 1:])

        lidarData, labels = get_filtered_lidar(lidarData, cnf.boundary, labels)

        bev_map = makeBEVMap(lidarData, cnf.boundary)
        bev_map = torch.from_numpy(bev_map)

        hflipped = False
        if np.random.random() < self.hflip_prob:
            hflipped = True
            # C, H, W
            bev_map = torch.flip(bev_map, [-1])

        targets = self.build_targets(labels, hflipped)

        metadatas = {
            'img_path': img_path,
            'hflipped': hflipped
        }

        return metadatas, bev_map, targets

    def get_image(self, idx):
        img_path = os.path.join(self.image_dir, '{:06d}.png'.format(idx))
        img = cv2.cvtColor(cv2.imread(img_path), cv2.COLOR_BGR2RGB)

        return img_path, img

    def get_calib(self, idx):
        calib_file = os.path.join(self.calib_dir, '{:06d}.txt'.format(idx))
        # assert os.path.isfile(calib_file)
        return Calibration(calib_file)

    def get_lidar(self, idx):
        lidar_file = os.path.join(self.lidar_dir, '{:06d}.bin'.format(idx))
        # assert os.path.isfile(lidar_file)
        return np.fromfile(lidar_file, dtype=np.float32).reshape(-1, 4)

    def get_label(self, idx):
        labels = []
        label_path = os.path.join(self.label_dir, '{:06d}.txt'.format(idx))
        for line in open(label_path, 'r'):
            line = line.rstrip()
            line_parts = line.split(' ')
            obj_name = line_parts[0]  # 'Car', 'Pedestrian', ...
            cat_id = int(cnf.CLASS_NAME_TO_ID[obj_name])
            if cat_id <= -99:  # ignore Tram and Misc
                continue
            truncated = int(float(line_parts[1]))  # truncated pixel ratio [0..1]
            occluded = int(line_parts[2])  # 0=visible, 1=partly occluded, 2=fully occluded, 3=unknown
            alpha = float(line_parts[3])  # object observation angle [-pi..pi]
            # xmin, ymin, xmax, ymax
            bbox = np.array([float(line_parts[4]), float(line_parts[5]), float(line_parts[6]), float(line_parts[7])])
            # height, width, length (h, w, l)
            h, w, l = float(line_parts[8]), float(line_parts[9]), float(line_parts[10])
            # location (x,y,z) in camera coord.
            x, y, z = float(line_parts[11]), float(line_parts[12]), float(line_parts[13])
            ry = float(line_parts[14])  # yaw angle (around Y-axis in camera coordinates) [-pi..pi]

            object_label = [cat_id, x, y, z, h, w, l, ry]
            labels.append(object_label)

        if len(labels) == 0:
            labels = np.zeros((1, 8), dtype=np.float32)
            has_labels = False
        else:
            labels = np.array(labels, dtype=np.float32)
            has_labels = True
            # Euclidean distance from sensor origin to object center (x, y, z)
        #distances = np.linalg.norm(labels[:, 1:4], axis=1, keepdims=True)  # shape: (N, 1)


        return labels, has_labels

    def build_targets(self, labels, hflipped):
        minX = cnf.boundary['minX']
        maxX = cnf.boundary['maxX']
        minY = cnf.boundary['minY']
        maxY = cnf.boundary['maxY']
        minZ = cnf.boundary['minZ']
        maxZ = cnf.boundary['maxZ']

        num_objects = min(len(labels), self.max_objects)
        hm_l, hm_w = self.hm_size

        hm_main_center = np.zeros((self.num_classes, hm_l, hm_w), dtype=np.float32)
        cen_offset = np.zeros((self.max_objects, 2), dtype=np.float32)
        direction = np.zeros((self.max_objects, 2), dtype=np.float32)
        z_coor = np.zeros((self.max_objects, 1), dtype=np.float32)
        dimension = np.zeros((self.max_objects, 3), dtype=np.float32)

        indices_center = np.zeros((self.max_objects), dtype=np.int64)
        obj_mask = np.zeros((self.max_objects), dtype=np.uint8)

        distance_map = np.zeros((self.max_objects, 1), dtype=np.float32)
        
        for k in range(num_objects):
            
            cls_id, x, y, z, h, w, l, yaw = labels[k]
            cls_id = int(cls_id)
            # Invert yaw angle
            yaw = -yaw
            if not ((minX <= x <= maxX) and (minY <= y <= maxY) and (minZ <= z <= maxZ)):
                continue
            if (h <= 0) or (w <= 0) or (l <= 0):
                continue

            bbox_l = l / cnf.bound_size_x * hm_l
            bbox_w = w / cnf.bound_size_y * hm_w
            radius = compute_radius((math.ceil(bbox_l), math.ceil(bbox_w)))
            radius = max(0, int(radius))

            center_y = (x - minX) / cnf.bound_size_x * hm_l  # x --> y (invert to 2D image space)
            center_x = (y - minY) / cnf.bound_size_y * hm_w  # y --> x
            center = np.array([center_x, center_y], dtype=np.float32)

            if hflipped:
                center[0] = hm_w - center[0] - 1

            center_int = center.astype(np.int32)
            if cls_id < 0:
                ignore_ids = [_ for _ in range(self.num_classes)] if cls_id == - 1 else [- cls_id - 2]
                # Consider to make mask ignore
                for cls_ig in ignore_ids:
                    gen_hm_radius(hm_main_center[cls_ig], center_int, radius)
                hm_main_center[ignore_ids, center_int[1], center_int[0]] = 0.9999
                continue

            # Generate heatmaps for main center
            gen_hm_radius(hm_main_center[cls_id], center, radius)
            # Index of the center
            indices_center[k] = center_int[1] * hm_w + center_int[0]

            # targets for center offset
            cen_offset[k] = center - center_int

            # targets for dimension
            dimension[k, 0] = h
            dimension[k, 1] = w
            dimension[k, 2] = l

            # targets for direction
            direction[k, 0] = math.sin(float(yaw))  # im
            direction[k, 1] = math.cos(float(yaw))  # re
            # im -->> -im
            if hflipped:
                direction[k, 0] = - direction[k, 0]

            # targets for depth
            z_coor[k] = z - minZ

            # target for distance (absolute from origin)
            #distance_map[k] = np.linalg.norm([x, y, z])


            # Generate object masks
            obj_mask[k] = 1
            

        targets = {
            'hm_cen': hm_main_center,
            'cen_offset': cen_offset,
            'direction': direction,
            'z_coor': z_coor,
            'dim': dimension,
            'indices_center': indices_center,
            'obj_mask': obj_mask
        }
        

        return targets

    def draw_img_with_label(self, index):
        sample_id = int(self.sample_id_list[index])
        img_path, img_rgb = self.get_image(sample_id)
        lidarData = self.get_lidar(sample_id)
        calib = self.get_calib(sample_id)
        print(img_path)
        labels, has_labels = self.get_label(sample_id)
        if has_labels:
            labels[:, 1:] = transformation.camera_to_lidar_box(labels[:, 1:], calib.V2C, calib.R0, calib.P2)

        if self.lidar_aug:
            lidarData, labels[:, 1:] = self.lidar_aug(lidarData, labels[:, 1:])

        lidarData, labels = get_filtered_lidar(lidarData, cnf.boundary, labels)
        bev_map = makeBEVMap(lidarData, cnf.boundary)

        return bev_map, labels, img_rgb, img_path


if __name__ == '__main__':
    from easydict import EasyDict as edict
    from data_process.transformation import OneOf, Random_Scaling, Random_Rotation, lidar_to_camera_box
    from utils.visualization_utils import merge_rgb_to_bev, show_rgb_image_with_boxes

    configs = edict()
    configs.distributed = False  # For testing
    configs.pin_memory = False
    configs.num_samples = None
    configs.input_size = (608, 608)
    configs.hm_size = (152, 152)
    configs.max_objects = 50
    configs.num_classes = 3
    configs.output_width = 608

    configs.dataset_dir = os.path.join('../../', 'dataset', 'kitti')
    # lidar_aug = OneOf([
    #     Random_Rotation(limit_angle=np.pi / 4, p=1.),
    #     Random_Scaling(scaling_range=(0.95, 1.05), p=1.),
    # ], p=1.)
    lidar_aug = None

    dataset = KittiDataset(configs, mode='val', lidar_aug=lidar_aug, hflip_prob=0., num_samples=configs.num_samples)

    print('\n\nPress n to see the next sample >>> Press Esc to quit...')
    centers = []
    save_folder = '/home/mlkr_a/virtual_environment_python3/SFA3D/results/'
    if not os.path.exists(save_folder):
        os.makedirs(save_folder)

    for idx in range(len(dataset)):
        try:
            bev_map, labels, img_rgb, img_path = dataset.draw_img_with_label(idx)
        except FileNotFoundError:
            print(f"File not found: {idx}")
            continue
        all_data = []
        calib = Calibration(img_path.replace(".png", ".txt").replace("image_2", "calib"))
        bev_map = (bev_map.transpose(1, 2, 0) * 255).astype(np.uint8)
        bev_map = cv2.resize(bev_map, (cnf.BEV_HEIGHT, cnf.BEV_WIDTH))
        fn_to_be_saved = img_path[-10:-3]+'csv'

        for box_idx, (cls_id, x, y, z, h, w, l, yaw) in enumerate(labels):
            # Draw rotated box
            yaw = -yaw
            y1 = int((x - cnf.boundary['minX']) / cnf.DISCRETIZATION)
            x1 = int((y - cnf.boundary['minY']) / cnf.DISCRETIZATION)
            w1 = int(w / cnf.DISCRETIZATION)
            l1 = int(l / cnf.DISCRETIZATION)
            if cls_id == 1:
                centers = drawRotatedBox(bev_map, x1, y1, w1, l1, yaw, cnf.colors[int(cls_id)])
                flat =centers.flatten()

            #drawRotatedBox(bev_map, x1, y1, w1, l1, yaw, cnf.colors[int(cls_id)])
        # Rotate the bev_map
        #bev_map = cv2.rotate(bev_map, cv2.ROTATE_180)

        labels[:, 1:] = lidar_to_camera_box(labels[:, 1:], calib.V2C, calib.R0, calib.P2)
        img_rgb = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        img_rgb, dist = show_rgb_image_with_boxes(img_rgb, labels, calib)

        out_img = merge_rgb_to_bev(img_rgb, bev_map, output_width=configs.output_width)
        cv2.imshow('bev_map', out_img)

        if cv2.waitKey(0) & 0xff == 27:
            break

