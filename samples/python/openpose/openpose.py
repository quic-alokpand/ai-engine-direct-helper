# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
import numpy as np
import math
import torch
import PIL
import torchvision.transforms as transforms

from PIL import Image
from PIL.Image import fromarray as ImageFromArray
import torch.nn.functional as F
from torch.nn.functional import interpolate, pad
from torchvision import transforms
from typing import Callable, Dict, List, Tuple
from scipy.ndimage.filters import gaussian_filter

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

openpose = None


def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.PILToTensor()])  # bgr image
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.permute(1, 2, 0).detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)

def resize_pad(image: torch.Tensor, dst_size: Tuple[int, int]):
    """
    Resize and pad image to be shape [..., dst_size[0], dst_size[1]]

    Parameters:
        image: (..., H, W)
            Image to reshape.

        dst_size: (height, width)
            Size to which the image should be reshaped.

    Returns:
        rescaled_padded_image: torch.Tensor (..., dst_size[0], dst_size[1])
        scale: scale factor between original image and dst_size image, (w, h)
        pad: pixels of padding added to the rescaled image: (left_padding, top_padding)

    Based on https://github.com/zmurez/MediaPipePyTorch/blob/master/blazebase.py
    """
    height, width = image.shape[-2:]
    dst_frame_height, dst_frame_width = dst_size

    h_ratio = dst_frame_height / height
    w_ratio = dst_frame_width / width
    scale = min(h_ratio, w_ratio)
    if h_ratio < w_ratio:
        scale = h_ratio
        new_height = dst_frame_height
        new_width = math.floor(width * scale)
    else:
        scale = w_ratio
        new_height = math.floor(height * scale)
        new_width = dst_frame_width

    new_height = math.floor(height * scale)
    new_width = math.floor(width * scale)
    pad_h = dst_frame_height - new_height
    pad_w = dst_frame_width - new_width

    pad_top = int(pad_h // 2)
    pad_bottom = int(pad_h // 2 + pad_h % 2)
    pad_left = int(pad_w // 2)
    pad_right = int(pad_w // 2 + pad_w % 2)

    rescaled_image = interpolate(
        image, size=[int(new_height), int(new_width)], mode="bilinear"
    )
    rescaled_padded_image = pad(
        rescaled_image, (pad_left, pad_right, pad_top, pad_bottom)
    )
    padding = (pad_left, pad_top)

    return rescaled_padded_image, scale, padding

def undo_resize_pad(
    image: torch.Tensor,
    orig_size_wh: Tuple[int, int],
    scale: float,
    padding: Tuple[int, int],
):
    """
    Undos the efffect of resize_pad. Instead of scale, the original size
    (in order width, height) is provided to prevent an off-by-one size.
    """
    width, height = orig_size_wh

    rescaled_image = interpolate(image, scale_factor=1 / scale, mode="bilinear")

    scaled_padding = [int(round(padding[0] / scale)), int(round(padding[1] / scale))]

    cropped_image = rescaled_image[
        ...,
        scaled_padding[1] : scaled_padding[1] + height,
        scaled_padding[0] : scaled_padding[0] + width,
    ]

    return cropped_image

def pil_resize_pad(
    image: Image, dst_size: Tuple[int, int]
) -> Tuple[Image, float, Tuple[int, int]]:
    torch_image = preprocess_PIL_image(image)
    torch_out_image, scale, padding = resize_pad(
        torch_image,
        dst_size,
    )
    pil_out_image = torch_tensor_to_PIL_image(torch_out_image[0])
    return (pil_out_image, scale, padding)

def pil_undo_resize_pad(
    image: Image, orig_size_wh: Tuple[int, int], scale: float, padding: Tuple[int, int]
) -> Image:
    torch_image = preprocess_PIL_image(image)
    torch_out_image = undo_resize_pad(torch_image, orig_size_wh, scale, padding)
    pil_out_image = torch_tensor_to_PIL_image(torch_out_image[0])
    return pil_out_image

def getKeypointsFromPredictions(
    paf: torch.Tensor, heatmap: torch.Tensor, h, w
) -> Tuple[np.ndarray, np.ndarray]:
    # upsample the PAF and heatmap to be the same size as the original image
    target_size = (h, w)
    upsampled_paf = (
        F.interpolate(paf, size=target_size, mode="bicubic", align_corners=False)
        .detach()
        .numpy()
    )
    heatmap = (
        F.interpolate(heatmap, size=target_size, mode="bicubic", align_corners=False)
        .detach()
        .numpy()
    )

    # reshape for post processing
    heatmap = np.transpose(heatmap.squeeze(), (1, 2, 0))
    paf = np.transpose(upsampled_paf.squeeze(), (1, 2, 0))
    
    """
    The following post-processing code comes from the pytorch openpose repo, at
    https://github.com/Hzzone/pytorch-openpose/blob/5ee71dc10020403dc3def2bb68f9b77c40337ae2/src/body.py#L67C9-L67C9
    """

    all_peaks = []
    peak_counter = 0
    thre1 = 0.1
    thre2 = 0.05

    for part in range(18):
        map_ori = heatmap[:, :, part]
        one_heatmap = gaussian_filter(map_ori, sigma=3)

        map_left = np.zeros(one_heatmap.shape)
        map_left[1:, :] = one_heatmap[:-1, :]
        map_right = np.zeros(one_heatmap.shape)
        map_right[:-1, :] = one_heatmap[1:, :]
        map_up = np.zeros(one_heatmap.shape)
        map_up[:, 1:] = one_heatmap[:, :-1]
        map_down = np.zeros(one_heatmap.shape)
        map_down[:, :-1] = one_heatmap[:, 1:]

        peaks_binary = np.logical_and.reduce(
            (
                one_heatmap >= map_left,
                one_heatmap >= map_right,
                one_heatmap >= map_up,
                one_heatmap >= map_down,
                one_heatmap > thre1,
            )
        )
        peaks = list(
            zip(np.nonzero(peaks_binary)[1], np.nonzero(peaks_binary)[0])
        )  # note reverse
        peaks_with_score = [x + (map_ori[x[1], x[0]],) for x in peaks]
        peak_id = range(peak_counter, peak_counter + len(peaks))
        peaks_with_score_and_id = [
            peaks_with_score[i] + (peak_id[i],) for i in range(len(peak_id))
        ]

        all_peaks.append(peaks_with_score_and_id)
        peak_counter += len(peaks)

    # find connection in the specified sequence, center 29 is in the position 15
    limbSeq = [
        [2, 3],
        [2, 6],
        [3, 4],
        [4, 5],
        [6, 7],
        [7, 8],
        [2, 9],
        [9, 10],
        [10, 11],
        [2, 12],
        [12, 13],
        [13, 14],
        [2, 1],
        [1, 15],
        [15, 17],
        [1, 16],
        [16, 18],
        [3, 17],
        [6, 18],
    ]
    # the middle joints heatmap correpondence
    mapIdx = [
        [31, 32],
        [39, 40],
        [33, 34],
        [35, 36],
        [41, 42],
        [43, 44],
        [19, 20],
        [21, 22],
        [23, 24],
        [25, 26],
        [27, 28],
        [29, 30],
        [47, 48],
        [49, 50],
        [53, 54],
        [51, 52],
        [55, 56],
        [37, 38],
        [45, 46],
    ]

    connection_all = []
    special_k = []
    mid_num = 10

    for k in range(len(mapIdx)):
        score_mid = paf[:, :, [x - 19 for x in mapIdx[k]]]
        candA = all_peaks[limbSeq[k][0] - 1]
        candB = all_peaks[limbSeq[k][1] - 1]
        nA = len(candA)
        nB = len(candB)
        indexA, indexB = limbSeq[k]
        if nA != 0 and nB != 0:
            connection_candidate = []
            for i in range(nA):
                for j in range(nB):
                    vec = np.subtract(candB[j][:2], candA[i][:2])
                    norm = math.sqrt(vec[0] * vec[0] + vec[1] * vec[1])
                    norm = max(0.001, norm)
                    vec = np.divide(vec, norm)

                    startend = list(
                        zip(
                            np.linspace(candA[i][0], candB[j][0], num=mid_num),
                            np.linspace(candA[i][1], candB[j][1], num=mid_num),
                        )
                    )

                    vec_x = np.array(
                        [
                            score_mid[
                                int(round(startend[index][1])),
                                int(round(startend[index][0])),
                                0,
                            ]
                            for index in range(len(startend))
                        ]
                    )
                    vec_y = np.array(
                        [
                            score_mid[
                                int(round(startend[index][1])),
                                int(round(startend[index][0])),
                                1,
                            ]
                            for index in range(len(startend))
                        ]
                    )

                    score_midpts = np.multiply(vec_x, vec[0]) + np.multiply(
                        vec_y, vec[1]
                    )
                    score_with_dist_prior = sum(score_midpts) / len(score_midpts) + min(
                        0.5 * h / norm - 1, 0
                    )
                    criterion1 = len(np.nonzero(score_midpts > thre2)[0]) > 0.8 * len(
                        score_midpts
                    )
                    criterion2 = score_with_dist_prior > 0
                    if criterion1 and criterion2:
                        connection_candidate.append(
                            [
                                i,
                                j,
                                score_with_dist_prior,
                                score_with_dist_prior + candA[i][2] + candB[j][2],
                            ]
                        )

            connection_candidate = sorted(
                connection_candidate, key=lambda x: x[2], reverse=True
            )
            connection = np.zeros((0, 5))
            for c in range(len(connection_candidate)):
                i, j, s = connection_candidate[c][0:3]
                if i not in connection[:, 3] and j not in connection[:, 4]:
                    connection = np.vstack(
                        [connection, [candA[i][3], candB[j][3], s, i, j]]
                    )
                    if len(connection) >= min(nA, nB):
                        break

            connection_all.append(connection)
        else:
            special_k.append(k)
            connection_all.append([])

    # last number in each row is the total parts number of that person
    # the second last number in each row is the score of the overall configuration
    subset = -1 * np.ones((0, 20))
    candidate = np.array([item for sublist in all_peaks for item in sublist])

    for k in range(len(mapIdx)):
        if k not in special_k:
            partAs = connection_all[k][:, 0]
            partBs = connection_all[k][:, 1]
            indexA, indexB = np.array(limbSeq[k]) - 1

            for i in range(len(connection_all[k])):  # = 1:size(temp,1)
                found = 0
                subset_idx = [-1, -1]
                for j in range(len(subset)):  # 1:size(subset,1):
                    if subset[j][indexA] == partAs[i] or subset[j][indexB] == partBs[i]:
                        subset_idx[found] = j
                        found += 1

                if found == 1:
                    j = subset_idx[0]
                    if subset[j][indexB] != partBs[i]:
                        subset[j][indexB] = partBs[i]
                        subset[j][-1] += 1
                        subset[j][-2] += (
                            candidate[partBs[i].astype(int), 2]
                            + connection_all[k][i][2]
                        )
                elif found == 2:  # if found 2 and disjoint, merge them
                    j1, j2 = subset_idx
                    membership = (
                        (subset[j1] >= 0).astype(int) + (subset[j2] >= 0).astype(int)
                    )[:-2]
                    if len(np.nonzero(membership == 2)[0]) == 0:  # merge
                        subset[j1][:-2] += subset[j2][:-2] + 1
                        subset[j1][-2:] += subset[j2][-2:]
                        subset[j1][-2] += connection_all[k][i][2]
                        subset = np.delete(subset, j2, 0)
                    else:  # as like found == 1
                        subset[j1][indexB] = partBs[i]
                        subset[j1][-1] += 1
                        subset[j1][-2] += (
                            candidate[partBs[i].astype(int), 2]
                            + connection_all[k][i][2]
                        )

                # if find no partA in the subset, create a new subset
                elif not found and k < 17:
                    row = -1 * np.ones(20)
                    row[indexA] = partAs[i]
                    row[indexB] = partBs[i]
                    row[-1] = 2
                    row[-2] = (
                        sum(candidate[connection_all[k][i, :2].astype(int), 2])
                        + connection_all[k][i][2]
                    )
                    subset = np.vstack([subset, row])
    # delete some rows of subset which has few parts occur
    deleteIdx = []
    for i in range(len(subset)):
        if subset[i][-1] < 4 or subset[i][-2] / subset[i][-1] < 0.4:
            deleteIdx.append(i)
    subset = np.delete(subset, deleteIdx, axis=0)

    # subset: n*20 array, 0-17 is the index in candidate, 18 is the total score, 19 is the total parts
    # candidate: x, y, score, id
    return candidate, subset


def draw_keypoints(image: Image, keypoints: np.ndarray, radius=1, alpha=1.0):
    overlay = image.copy()
    draw = PIL.ImageDraw.Draw(overlay)
    confidence_threshold = 0.8
    for kp in keypoints:
        x, y, v, i = kp
        if v > confidence_threshold:
            draw.ellipse(
                (
                    (int(x - radius), int(y - radius)),
                    (int(x + radius), int(y + radius)),
                ),
                outline=(0, 255, 0),
                fill=(0, 255, 0),
            )

    return PIL.Image.blend(overlay, image, alpha)

# OpenPose class which inherited from the class QNNContext.
class OpenPose(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)
        return output_data
        
def Init():
    global openpose

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qnn", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for OpnPose objects.
    openpose_model = "models\\openpose.bin"
    openpose = OpenPose("openpose", openpose_model)

def Inference(input_image_path, output_image_path): 
    # Load image
    orig_image = Image.open(input_image_path)
    image, scale, padding = pil_resize_pad(orig_image, (224, 224))
       
    # preprocess
    pixel_tensor = preprocess_PIL_image(image).numpy()
    pixel_values = np.transpose(pixel_tensor, (0, 2, 3, 1))
   
    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    
    # Run prediction
    model_output = openpose.Inference([pixel_values])
    
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # postprocess the result
    paf = model_output[0]
    heatmap = model_output[1]
    
    paf = paf.reshape(1, 28, 28, 38)
    heatmap = heatmap.reshape(1, 28, 28, 19)  
    
    paf_tensor = torch.from_numpy(paf)
    heatmap_tensor = torch.from_numpy(heatmap)
    
    paf_tensor = paf_tensor.permute(0, 3, 1, 2)
    heatmap_tensor = heatmap_tensor.permute(0, 3, 1, 2)
   
    # post process heatmaps and paf to get keypoints
    keypoints, subset = getKeypointsFromPredictions(
        paf_tensor, heatmap_tensor, pixel_tensor.shape[2], pixel_tensor.shape[3]
    )

    output_image = draw_keypoints(image, keypoints, radius=4, alpha=0.8)

    # Resize / unpad annotated image
    pred_image = pil_undo_resize_pad(output_image, orig_image.size, scale, padding)
    
    # show&save the result
    pred_image.save(output_image_path)
    pred_image.show()
    
  
def Release():
    global openpose

    # Release the resources.
    del(openpose)


Init()

Inference("input.png", "output.png")

Release()
