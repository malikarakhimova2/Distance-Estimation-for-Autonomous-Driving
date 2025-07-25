"""
# -*- coding: utf-8 -*-
-----------------------------------------------------------------------------------
# Author: Nguyen Mau Dung
# DoC: 2020.08.17
# email: nguyenmaudung93.kstn@gmail.com
-----------------------------------------------------------------------------------
# Description: The utils for evaluation
# Refer from: https://github.com/xingyizhou/CenterNet
"""

from __future__ import division
import os
import sys

import torch
import numpy as np
import torch.nn.functional as F
import cv2

src_dir = os.path.dirname(os.path.realpath(__file__))
while not src_dir.endswith("sfa"):
    src_dir = os.path.dirname(src_dir)
if src_dir not in sys.path:
    sys.path.append(src_dir)

import config.kitti_config as cnf
from data_process import transformation 
from data_process.kitti_data_utils import Calibration, get_filtered_lidar
from data_process.kitti_bev_utils import drawRotatedBox, get_corners
from data_process.tracker import Tracker
from scipy.optimize import linear_sum_assignment


def _nms(heat, kernel=3):
    pad = (kernel - 1) // 2
    hmax = F.max_pool2d(heat, (kernel, kernel), stride=1, padding=pad)
    keep = (hmax == heat).float()

    return heat * keep


def _gather_feat(feat, ind, mask=None):
    dim = feat.size(2)
    ind = ind.unsqueeze(2).expand(ind.size(0), ind.size(1), dim)
    feat = feat.gather(1, ind)
    if mask is not None:
        mask = mask.unsqueeze(2).expand_as(feat)
        feat = feat[mask]
        feat = feat.view(-1, dim)
    return feat


def _transpose_and_gather_feat(feat, ind):
    feat = feat.permute(0, 2, 3, 1).contiguous()
    feat = feat.view(feat.size(0), -1, feat.size(3))
    feat = _gather_feat(feat, ind)
    return feat


def _topk(scores, K=40):
    batch, cat, height, width = scores.size()

    topk_scores, topk_inds = torch.topk(scores.view(batch, cat, -1), K)

    topk_inds = topk_inds % (height * width)
    topk_ys = (torch.floor_divide(topk_inds, width)).float()
    topk_xs = (topk_inds % width).int().float()

    topk_score, topk_ind = torch.topk(topk_scores.view(batch, -1), K)
    topk_clses = (torch.floor_divide(topk_ind, K)).int()
    topk_inds = _gather_feat(topk_inds.view(batch, -1, 1), topk_ind).view(batch, K)
    topk_ys = _gather_feat(topk_ys.view(batch, -1, 1), topk_ind).view(batch, K)
    topk_xs = _gather_feat(topk_xs.view(batch, -1, 1), topk_ind).view(batch, K)

    return topk_score, topk_inds, topk_clses, topk_ys, topk_xs


def _topk_channel(scores, K=40):
    batch, cat, height, width = scores.size()

    topk_scores, topk_inds = torch.topk(scores.view(batch, cat, -1), K)

    topk_inds = topk_inds % (height * width)
    topk_ys = (topk_inds / width).int().float()
    topk_xs = (topk_inds % width).int().float()

    return topk_scores, topk_inds, topk_ys, topk_xs


def decode(hm_cen, cen_offset, direction, z_coor, dim, K=40):
    batch_size, num_classes, height, width = hm_cen.size()

    hm_cen = _nms(hm_cen)
    scores, inds, clses, ys, xs = _topk(hm_cen, K=K)

# Then gather distance if available
    #if distance is not None:
    #    distance = _transpose_and_gather_feat(distance, inds)
    #    distance = distance.view(batch_size, K, 1)
    #    detections = torch.cat([detections, distance], dim=2)


    if cen_offset is not None:
        cen_offset = _transpose_and_gather_feat(cen_offset, inds)
        cen_offset = cen_offset.view(batch_size, K, 2)
        xs = xs.view(batch_size, K, 1) + cen_offset[:, :, 0:1]
        ys = ys.view(batch_size, K, 1) + cen_offset[:, :, 1:2]
    else:
        xs = xs.view(batch_size, K, 1) + 0.5
        ys = ys.view(batch_size, K, 1) + 0.5

    direction = _transpose_and_gather_feat(direction, inds)
    direction = direction.view(batch_size, K, 2)
    z_coor = _transpose_and_gather_feat(z_coor, inds)
    z_coor = z_coor.view(batch_size, K, 1)
    dim = _transpose_and_gather_feat(dim, inds)
    dim = dim.view(batch_size, K, 3)
    clses = clses.view(batch_size, K, 1).float()
    scores = scores.view(batch_size, K, 1)

    # (scores x 1, ys x 1, xs x 1, z_coor x 1, dim x 3, direction x 2, clses x 1)
    # (scores-0:1, ys-1:2, xs-2:3, z_coor-3:4, dim-4:7, direction-7:9, clses-9:10)
    # detections: [batch_size, K, 10]
    #if distance is not None:
    #    detections = torch.cat([scores, xs, ys, z_coor, dim, direction, clses, distance], dim=2)
    #else:
    detections = torch.cat([scores, xs, ys, z_coor, dim, direction, clses], dim=2)


    return detections


def get_yaw(direction):
    return np.arctan2(direction[:, 0:1], direction[:, 1:2])


def post_processing(detections, num_classes=3, down_ratio=4, peak_thresh=0.2):
    """
    :param detections: [batch_size, K, 10]
    # (scores x 1, xs x 1, ys x 1, z_coor x 1, dim x 3, direction x 2, clses x 1)
    # (scores-0:1, xs-1:2, ys-2:3, z_coor-3:4, dim-4:7, direction-7:9, clses-9:10)
    :return:
    """
    # TODO: Need to consider rescale to the original scale: x, y

    ret = []
    for i in range(detections.shape[0]):
        top_preds = {}
        classes = detections[i, :, -1]
        for j in range(num_classes):
            inds = (classes == j)
            # x, y, z, h, w, l, yaw
            top_preds[j] = np.concatenate([
                detections[i, inds, 0:1],
                detections[i, inds, 1:2] * down_ratio,
                detections[i, inds, 2:3] * down_ratio,
                detections[i, inds, 3:4],
                detections[i, inds, 4:5],
                detections[i, inds, 5:6] / cnf.bound_size_y * cnf.BEV_WIDTH,
                detections[i, inds, 6:7] / cnf.bound_size_x * cnf.BEV_HEIGHT,
                get_yaw(detections[i, inds, 7:9]).astype(np.float32)], axis=1)
            # Filter by peak_thresh
            if len(top_preds[j]) > 0:
                keep_inds = (top_preds[j][:, 0] > peak_thresh)
                top_preds[j] = top_preds[j][keep_inds]
        ret.append(top_preds)

    return ret


def draw_predictions(img, detections, tracker=None, distance=None, filename=None ):
    track_colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
                    (0, 255, 255), (255, 0, 255), (255, 127, 255),
                    (127, 0, 255), (127, 0, 127), (50,50,98),(37,37,47)]
    print (distance)
    np.set_printoptions(formatter={'float': lambda x: "{:.4f}".format(x)})
    transformed_array = np.vstack([np.hstack((np.full((arr.shape[0], 1), cls_idx), arr)) for cls_idx, arr in detections.items()])
    detection_centers = []
    track_centers = []
    all_data = []
    save_folder = '/home/mlkr_a/virtual_environment_python3/SFA3D/results/'

    if not os.path.exists(save_folder):
        os.makedirs(save_folder)
    print(os.path.join(save_folder, (filename or 'unnamed') + '.txt'))

    for det in transformed_array:
        class_id, _score, _x, _y, _z, _h, _w, _l, _yaw = det
        #distance_text = f"{np.sqrt(_x**2 + _y**2 + _z**2):.1f}m"
        #center_pixel = (int(_x), int(_y))
        #cv2.putText(img, str(track.track_id), (int(x1), int(y1)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, clr, 2)
        if class_id == 1 and _score > 0.2:
            cnt = drawRotatedBox(img, _x, _y, _w, _l, _yaw, cnf.colors[int(class_id)])
            flat = cnt.flatten()
            #distance_m = round(_z + cnf.boundary['minZ'], 2)
            #label = f"{distance_m:.1f}m"
            #label_pos = (int(_x), int(_y) - 12)
            #cv2.putText(img,label, label_pos, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        #if hasattr(track, "track_id"):
        #    cv2.putText(img, str(track.track_id), (int(_x), int(_y) + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 2)

        # Tracker EKF
    tracker.update(detection_centers)
    valid_tracks = []
    for track in tracker.tracks:
        if 0 <= track.track_id % 6 < len(track_colors):
            valid_tracks.append(track)
        else:
            print("Invalid track ID:", track.track_id)
    tracker.tracks = valid_tracks
    for track in tracker.tracks:
            track_centers.append(track.KF.predict())

    if detection_centers and track_centers:
        cost_matrix = np.zeros((len(detection_centers), len(track_centers)))
        for i, det_center in enumerate(detection_centers):
            for j, track_center in enumerate(track_centers):
                cost_matrix[i, j] = np.linalg.norm(np.array(det_center) - np.array(track_center))
        detection_indices, track_indices = linear_sum_assignment(cost_matrix)

        for det_idx, track_idx in zip(detection_indices, track_indices):
            if det_idx < len(detection_centers) and track_idx < len(track_centers):
                
                detection_center = detection_centers[det_idx]
                track_center = track_centers[track_idx]
                

                # Update the track with the detected center using Kalman filter
                tracker.tracks[track_idx].KF.correct(np.array(detection_center), 1) # 1 is the dt

    for track in tracker.tracks:
        trace_length = len(track.trace)
        #print(trace_length)
        x1,y1,x2,y2 = 0,0,0,0
        pos1, pos2 = 0, 0
        clr_index = 0

        if trace_length > 1:
            for j in range(trace_length - 1):
                if j+1<trace_length:
                    y1, x1 = track.trace[j][0][0], track.trace[j][1][0]
                    y2, x2 = track.trace[j + 1][0][0], track.trace[j + 1][1][0]
                    clr_index = track.track_id % len(track_colors)
                    clr = track_colors[clr_index]
                    pos_index = min(2, trace_length - 1)
                    pos1, pos2 = track.trace[pos_index][1][0], track.trace[pos_index][0][0]
       
        # --- UNCOMMENT THE FOLLOWING LINES TO SHOW THE TRACKERS PERFORMANCE --- 
        # if clr_index < len(track_colors):  # Check if clr is within valid range
        #     #cv2.arrowedLine(img, (int(x1), int(y1)), (int(x2), int(y2)), track_colors[clr_index], thickness=2)
        #     #cv2.putText(img, str(track.track_id), (int(x1), int(y1)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, track_colors[clr_index], 2, cv2.LINE_AA)
        # else:
        #     print(f"Invalid clr index: {clr}")  # Debugging line


    return img


def convert_det_to_real_values(detections, num_classes=3):
    kitti_dets = []
    for cls_id in range(num_classes):
        if len(detections[cls_id]) > 0:
            for det in detections[cls_id]:
                # (scores-0:1, x-1:2, y-2:3, z-3:4, dim-4:7, yaw-7:8)
                _score, _x, _y, _z, _h, _w, _l, _yaw = det
                _yaw = -_yaw
                x = _y / cnf.BEV_HEIGHT * cnf.bound_size_x + cnf.boundary['minX']
                y = _x / cnf.BEV_WIDTH * cnf.bound_size_y + cnf.boundary['minY']
                z = _z + cnf.boundary['minZ']
                w = _w / cnf.BEV_WIDTH * cnf.bound_size_y
                l = _l / cnf.BEV_HEIGHT * cnf.bound_size_x

                kitti_dets.append([cls_id, x, y, z, _h, w, l, _yaw])

    return np.array(kitti_dets)
