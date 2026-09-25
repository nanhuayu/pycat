# Derived from RapidOCR 3.9.2 and PaddleOCR under Apache-2.0.
# Source revision: 095232a4c94f7f0e6600ba5bba1177010ad696d4.
# PyCat removes download/config/backend/CLI/visualization code, fixes the model
# profile to PP-OCRv6 small + ONNX Runtime CPU, and replaces Shapely polygon
# area/perimeter calls with equivalent OpenCV primitives.

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pyclipper
from onnxruntime import GraphOptimizationLevel, InferenceSession, SessionOptions


@dataclass(frozen=True)
class EngineBlock:
    text: str
    score: float
    box: tuple[tuple[int, int], ...]


class _OrtSession:
    def __init__(self, model_path: str | Path) -> None:
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        options = SessionOptions()
        options.log_severity_level = 4
        options.enable_cpu_mem_arena = False
        options.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = InferenceSession(
            str(path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [item.name for item in self.session.get_outputs()]

    def run(self, value: np.ndarray) -> np.ndarray:
        return self.session.run(self.output_names, {self.input_name: value})[0]

    def characters(self) -> list[str]:
        metadata = self.session.get_modelmeta().custom_metadata_map
        value = str(metadata.get("character") or "")
        if not value:
            raise ValueError("recognition model does not contain character metadata")
        return value.splitlines()


class _DetectionPreprocessor:
    def __init__(self, limit_side_len: int = 736) -> None:
        self.limit_side_len = int(limit_side_len)
        self.mean = np.array([0.5, 0.5, 0.5])
        self.std = np.array([0.5, 0.5, 0.5])

    def __call__(self, image: np.ndarray) -> np.ndarray | None:
        height, width = image.shape[:2]
        ratio = 1.0
        if min(height, width) < self.limit_side_len:
            ratio = self.limit_side_len / float(min(height, width))
        resize_height = int(round((height * ratio) / 32) * 32)
        resize_width = int(round((width * ratio) / 32) * 32)
        if resize_height <= 0 or resize_width <= 0:
            return None
        resized = cv2.resize(image, (resize_width, resize_height))
        normalized = (resized.astype("float32") / 255.0 - self.mean) / self.std
        return np.expand_dims(normalized.transpose((2, 0, 1)), axis=0).astype(np.float32)


class _DbPostProcessor:
    def __init__(self) -> None:
        self.thresh = 0.3
        self.box_thresh = 0.5
        self.max_candidates = 1000
        self.unclip_ratio = 1.6
        self.min_size = 3
        self.dilation_kernel = np.array([[1, 1], [1, 1]])

    def __call__(
        self,
        prediction: np.ndarray,
        original_shape: tuple[int, int],
    ) -> tuple[np.ndarray, list[float]]:
        source_height, source_width = original_shape
        prediction = prediction[:, 0, :, :]
        segmentation = prediction > self.thresh
        mask = cv2.dilate(segmentation[0].astype(np.uint8), self.dilation_kernel)
        boxes, scores = self._boxes_from_bitmap(
            prediction[0],
            mask,
            source_width,
            source_height,
        )
        return self._filter_boxes(boxes, scores, source_height, source_width)

    def _boxes_from_bitmap(
        self,
        prediction: np.ndarray,
        bitmap: np.ndarray,
        destination_width: int,
        destination_height: int,
    ) -> tuple[np.ndarray, list[float]]:
        height, width = bitmap.shape
        contour_result = cv2.findContours(
            (bitmap * 255).astype(np.uint8),
            cv2.RETR_LIST,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        contours = contour_result[-2]
        boxes: list[np.ndarray] = []
        scores: list[float] = []
        for contour in contours[: self.max_candidates]:
            points, short_side = self._mini_box(contour)
            if short_side < self.min_size:
                continue
            score = self._box_score_fast(prediction, points.reshape(-1, 2))
            if score < self.box_thresh:
                continue
            expanded = self._unclip(points)
            if expanded.size == 0:
                continue
            box, short_side = self._mini_box(expanded)
            if short_side < self.min_size + 2:
                continue
            box[:, 0] = np.clip(
                np.round(box[:, 0] / width * destination_width),
                0,
                destination_width,
            )
            box[:, 1] = np.clip(
                np.round(box[:, 1] / height * destination_height),
                0,
                destination_height,
            )
            boxes.append(box.astype(np.int32))
            scores.append(float(score))
        return np.array(boxes, dtype=np.int32), scores

    @staticmethod
    def _mini_box(contour: np.ndarray) -> tuple[np.ndarray, float]:
        bounding_box = cv2.minAreaRect(contour)
        points = sorted(list(cv2.boxPoints(bounding_box)), key=lambda point: point[0])
        if points[1][1] > points[0][1]:
            index_1, index_4 = 0, 1
        else:
            index_1, index_4 = 1, 0
        if points[3][1] > points[2][1]:
            index_2, index_3 = 2, 3
        else:
            index_2, index_3 = 3, 2
        box = np.array([points[index_1], points[index_2], points[index_3], points[index_4]])
        return box, min(bounding_box[1])

    @staticmethod
    def _box_score_fast(bitmap: np.ndarray, source_box: np.ndarray) -> float:
        height, width = bitmap.shape[:2]
        box = source_box.copy()
        x_min = np.clip(np.floor(box[:, 0].min()).astype(np.int32), 0, width - 1)
        x_max = np.clip(np.ceil(box[:, 0].max()).astype(np.int32), 0, width - 1)
        y_min = np.clip(np.floor(box[:, 1].min()).astype(np.int32), 0, height - 1)
        y_max = np.clip(np.ceil(box[:, 1].max()).astype(np.int32), 0, height - 1)
        mask = np.zeros((y_max - y_min + 1, x_max - x_min + 1), dtype=np.uint8)
        box[:, 0] -= x_min
        box[:, 1] -= y_min
        cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
        return float(cv2.mean(bitmap[y_min : y_max + 1, x_min : x_max + 1], mask)[0])

    def _unclip(self, box: np.ndarray) -> np.ndarray:
        contour = np.asarray(box, dtype=np.float32).reshape((-1, 1, 2))
        area = abs(float(cv2.contourArea(contour)))
        perimeter = float(cv2.arcLength(contour, True))
        if area <= 0 or perimeter <= 0:
            return np.empty((0, 1, 2), dtype=np.float32)
        distance = area * self.unclip_ratio / perimeter
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(box.tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        expanded_paths = offset.Execute(distance)
        if not expanded_paths:
            return np.empty((0, 1, 2), dtype=np.float32)
        expanded = max(
            expanded_paths,
            key=lambda path: abs(float(cv2.contourArea(np.asarray(path, dtype=np.float32)))),
        )
        return np.asarray(expanded, dtype=np.float32).reshape((-1, 1, 2))

    def _filter_boxes(
        self,
        boxes: np.ndarray,
        scores: list[float],
        image_height: int,
        image_width: int,
    ) -> tuple[np.ndarray, list[float]]:
        kept_boxes: list[np.ndarray] = []
        kept_scores: list[float] = []
        for box, score in zip(boxes, scores):
            ordered = self._order_clockwise(box)
            ordered[:, 0] = np.clip(ordered[:, 0], 0, image_width - 1)
            ordered[:, 1] = np.clip(ordered[:, 1], 0, image_height - 1)
            rect_width = int(np.linalg.norm(ordered[0] - ordered[1]))
            rect_height = int(np.linalg.norm(ordered[0] - ordered[3]))
            if rect_width <= 3 or rect_height <= 3:
                continue
            kept_boxes.append(ordered)
            kept_scores.append(score)
        return np.array(kept_boxes), kept_scores

    @staticmethod
    def _order_clockwise(points: np.ndarray) -> np.ndarray:
        x_sorted = points[np.argsort(points[:, 0]), :]
        left = x_sorted[:2, :][np.argsort(x_sorted[:2, 1]), :]
        right = x_sorted[2:, :][np.argsort(x_sorted[2:, 1]), :]
        top_left, bottom_left = left
        top_right, bottom_right = right
        return np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32")


class PpOcrV6Engine:
    revision = "ppocrv6-small-onnx"

    def __init__(self, *, detection_model: str | Path, recognition_model: str | Path) -> None:
        self._detection = _OrtSession(detection_model)
        self._recognition = _OrtSession(recognition_model)
        self._det_preprocess = _DetectionPreprocessor(limit_side_len=736)
        self._det_postprocess = _DbPostProcessor()
        characters = list(self._recognition.characters())
        characters.append(" ")
        characters.insert(0, "blank")
        self._characters = characters
        self._recognition_batch_size = 6
        self._recognition_shape = (3, 48, 320)
        self._text_score = 0.5

    def recognize(self, image: np.ndarray) -> list[EngineBlock]:
        if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
            raise ValueError("OCR input must be a three-channel image")
        original_height, original_width = image.shape[:2]
        processed, ratio_height, ratio_width = self._resize_within_bounds(image)
        processed, padding_top = self._vertical_padding(processed)

        detection_input = self._det_preprocess(processed)
        if detection_input is None:
            return []
        prediction = self._detection.run(detection_input)
        boxes, _scores = self._det_postprocess(prediction, processed.shape[:2])
        if len(boxes) == 0:
            return []
        boxes = self._sort_boxes(boxes)
        crops = [self._rotate_crop(processed, box.astype(np.float32)) for box in boxes]
        recognition = self._recognize_crops(crops)

        blocks: list[EngineBlock] = []
        for box, (text, score) in zip(boxes, recognition):
            clean_text = str(text or "").strip()
            if not clean_text or score < self._text_score:
                continue
            mapped = box.astype(np.float32).copy()
            mapped[:, 1] -= padding_top
            mapped[:, 0] *= ratio_width
            mapped[:, 1] *= ratio_height
            mapped[:, 0] = np.clip(mapped[:, 0], 0, original_width)
            mapped[:, 1] = np.clip(mapped[:, 1], 0, original_height)
            block_box = tuple(
                (int(round(float(point[0]))), int(round(float(point[1]))))
                for point in mapped
            )
            blocks.append(EngineBlock(clean_text, float(score), block_box))
        return blocks

    @staticmethod
    def _resize_within_bounds(image: np.ndarray) -> tuple[np.ndarray, float, float]:
        height, width = image.shape[:2]
        maximum_scale = 2000.0 / max(height, width)
        scale = min(1.0, maximum_scale)
        if min(height * scale, width * scale) < 30:
            scale = min(maximum_scale, max(scale, 30.0 / min(height, width)))
        resize_height = max(1, int(round(height * scale)))
        resize_width = max(1, int(round(width * scale)))
        if (resize_height, resize_width) == (height, width):
            return image, 1.0, 1.0
        resized = cv2.resize(image, (resize_width, resize_height))
        return resized, height / resize_height, width / resize_width

    @staticmethod
    def _vertical_padding(image: np.ndarray) -> tuple[np.ndarray, int]:
        height, width = image.shape[:2]
        if height > 30 and width / float(height) <= 8:
            return image, 0
        new_height = max(int(width / 8), 30) * 2
        padding = max(0, int(abs(new_height - height) / 2))
        padded = cv2.copyMakeBorder(
            image,
            padding,
            padding,
            0,
            0,
            cv2.BORDER_CONSTANT,
            value=(0, 0, 0),
        )
        return padded, padding

    @staticmethod
    def _sort_boxes(boxes: np.ndarray) -> np.ndarray:
        if len(boxes) == 0:
            return boxes
        y_coordinates = boxes[:, 0, 1]
        y_order = np.argsort(y_coordinates, kind="stable")
        y_sorted_boxes = boxes[y_order]
        y_sorted = y_coordinates[y_order]
        line_ids = np.concatenate(
            [[0], np.cumsum((np.diff(y_sorted) >= 10).astype(np.int32))]
        )
        x_coordinates = y_sorted_boxes[:, 0, 0]
        order = np.lexsort((x_coordinates, line_ids))
        return y_sorted_boxes[order]

    @staticmethod
    def _rotate_crop(image: np.ndarray, points: np.ndarray) -> np.ndarray:
        crop_width = max(
            1,
            int(
                max(
                    np.linalg.norm(points[0] - points[1]),
                    np.linalg.norm(points[2] - points[3]),
                )
            ),
        )
        crop_height = max(
            1,
            int(
                max(
                    np.linalg.norm(points[0] - points[3]),
                    np.linalg.norm(points[1] - points[2]),
                )
            ),
        )
        destination = np.array(
            [[0, 0], [crop_width, 0], [crop_width, crop_height], [0, crop_height]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(points, destination)
        crop = cv2.warpPerspective(
            image,
            matrix,
            (crop_width, crop_height),
            borderMode=cv2.BORDER_REPLICATE,
            flags=cv2.INTER_CUBIC,
        )
        if crop.shape[0] / float(max(crop.shape[1], 1)) >= 1.5:
            crop = np.rot90(crop)
        return crop

    def _recognize_crops(self, images: list[np.ndarray]) -> list[tuple[str, float]]:
        if not images:
            return []
        width_ratios = [image.shape[1] / float(image.shape[0]) for image in images]
        indices = np.argsort(np.asarray(width_ratios))
        results: list[tuple[str, float]] = [("", 0.0)] * len(images)
        for begin in range(0, len(images), self._recognition_batch_size):
            end = min(len(images), begin + self._recognition_batch_size)
            image_height = self._recognition_shape[1]
            max_ratio = self._recognition_shape[2] / image_height
            for position in range(begin, end):
                image = images[indices[position]]
                max_ratio = max(max_ratio, image.shape[1] / float(image.shape[0]))
            batch = [
                self._normalize_recognition_image(images[indices[position]], max_ratio)[np.newaxis, :]
                for position in range(begin, end)
            ]
            predictions = self._recognition.run(np.concatenate(batch).astype(np.float32))
            decoded = self._decode(predictions)
            for offset, item in enumerate(decoded):
                results[indices[begin + offset]] = item
        return results

    def _normalize_recognition_image(self, image: np.ndarray, max_ratio: float) -> np.ndarray:
        channels, image_height, _image_width = self._recognition_shape
        image_width = int(image_height * max_ratio)
        height, width = image.shape[:2]
        resized_width = min(image_width, int(math.ceil(image_height * width / float(height))))
        resized = cv2.resize(image, (max(1, resized_width), image_height)).astype("float32")
        resized = resized.transpose((2, 0, 1)) / 255.0
        resized = (resized - 0.5) / 0.5
        padded = np.zeros((channels, image_height, image_width), dtype=np.float32)
        padded[:, :, : resized.shape[2]] = resized
        return padded

    def _decode(self, predictions: np.ndarray) -> list[tuple[str, float]]:
        token_indices = predictions.argmax(axis=2)
        token_probabilities = predictions.max(axis=2)
        results: list[tuple[str, float]] = []
        for indices, probabilities in zip(token_indices, token_probabilities):
            selection = np.ones(len(indices), dtype=bool)
            selection[1:] = indices[1:] != indices[:-1]
            selection &= indices != 0
            selected_indices = indices[selection]
            selected_probabilities = probabilities[selection]
            valid = selected_indices < len(self._characters)
            selected_indices = selected_indices[valid]
            selected_probabilities = selected_probabilities[valid]
            text = "".join(self._characters[int(index)] for index in selected_indices)
            score = float(np.mean(selected_probabilities)) if len(selected_probabilities) else 0.0
            results.append((text, round(score, 5)))
        return results
