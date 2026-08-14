#!/usr/bin/env python3
"""Crash-isolated local PaddleOCR worker.

PaddlePaddle is deliberately imported only in this helper process.  Nautilus
communicates with it using line-delimited JSON, so a native inference failure
cannot take the file manager down with it.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

_AUTO_LAYOUT_MIN_LINES = 4
_AUTO_LAYOUT_MIN_CHARACTERS = 40
_LAYOUT_THRESHOLD = 0.35


def _emit(message: dict[str, Any]) -> None:
    print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)


def _runtime_config(runtime_root: Path) -> dict[str, Any]:
    path = runtime_root / "runtime.json"
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict) or config.get("schema_version") != 2:
        raise RuntimeError(f"Unsupported PaddleOCR runtime metadata in {path}")
    return config


def _model_directory(model_root: Path, name: str) -> Path:
    direct = model_root / "official_models" / name
    if direct.is_dir():
        return direct
    matches = [path for path in model_root.rglob(name) if path.is_dir()]
    if len(matches) != 1:
        raise RuntimeError(f"Could not resolve downloaded PaddleOCR model {name!r}")
    return matches[0]


def _make_pipeline(runtime_root: Path, config: dict[str, Any] | None = None):
    # These must be set before importing PaddleOCR/PaddleX.  The cache is owned
    # by this versioned runtime and the source probe is unnecessary once setup
    # has downloaded and validated both models.
    model_root = runtime_root / "models"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(model_root)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "true"
    os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")

    from paddleocr import PaddleOCR

    metadata = config or _runtime_config(runtime_root)
    models = metadata["models"]
    detection_name = str(models["detection_name"])
    recognition_name = str(models["recognition_name"])
    layout_dir = runtime_root / str(models["layout_dir"])
    detection_dir = runtime_root / str(models["detection_dir"])
    recognition_dir = runtime_root / str(models["recognition_dir"])
    if not detection_dir.is_dir() or not recognition_dir.is_dir() or not layout_dir.is_dir():
        raise RuntimeError("PaddleOCR runtime is missing one or more local models")
    return PaddleOCR(
        text_detection_model_name=detection_name,
        text_detection_model_dir=str(detection_dir),
        text_recognition_model_name=recognition_name,
        text_recognition_model_dir=str(recognition_dir),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        # PaddlePaddle 3.3's PIR converter cannot run these PP-OCRv5 models
        # through oneDNN (ArrayAttribute<DoubleAttribute>, upstream #77340).
        # The generic CPU kernels are slower but stable and remain isolated.
        enable_mkldnn=False,
        device="cpu",
    )


def _make_layout_pipeline(runtime_root: Path, config: dict[str, Any] | None = None):
    """Load layout detection only after a document-like request needs it."""
    metadata = config or _runtime_config(runtime_root)
    models = metadata["models"]
    layout_name = str(models["layout_name"])
    layout_dir = runtime_root / str(models["layout_dir"])
    if not layout_dir.is_dir():
        raise RuntimeError("PaddleOCR runtime is missing its layout model")

    from paddleocr import LayoutDetection

    return LayoutDetection(
        model_name=layout_name,
        model_dir=str(layout_dir),
        threshold=_LAYOUT_THRESHOLD,
        enable_mkldnn=False,
        device="cpu",
    )


def _result_payload(result: Any) -> dict[str, Any]:
    value = getattr(result, "json", result)
    if callable(value):
        value = value()
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        return {}
    nested = value.get("res")
    return nested if isinstance(nested, dict) else value


def _as_polygon(value: Any) -> list[list[float]] | None:
    try:
        points = [[float(point[0]), float(point[1])] for point in value]
    except (IndexError, TypeError, ValueError):
        return None
    return points if len(points) >= 4 else None


def _prediction_lines(results: Iterable[Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for result in results:
        payload = _result_payload(result)
        texts = payload.get("rec_texts")
        scores = payload.get("rec_scores")
        polygons = payload.get("rec_polys")
        if polygons is None:
            polygons = payload.get("dt_polys")
        texts = texts.tolist() if hasattr(texts, "tolist") else (texts or [])
        scores = scores.tolist() if hasattr(scores, "tolist") else (scores or [])
        polygons = polygons.tolist() if hasattr(polygons, "tolist") else (polygons or [])
        for index, text in enumerate(texts):
            clean_text = str(text).strip()
            polygon = _as_polygon(polygons[index]) if index < len(polygons) else None
            if not clean_text or polygon is None:
                continue
            try:
                score = float(scores[index]) if index < len(scores) else 1.0
            except (TypeError, ValueError):
                score = 0.0
            lines.append({"text": clean_text, "score": score, "polygon": polygon})
    return lines


def _as_box(value: Any) -> list[float] | None:
    value = value.tolist() if hasattr(value, "tolist") else value
    try:
        coordinates = [float(part) for part in value]
    except (TypeError, ValueError):
        return None
    if len(coordinates) == 8:
        xs = coordinates[0::2]
        ys = coordinates[1::2]
        coordinates = [min(xs), min(ys), max(xs), max(ys)]
    if len(coordinates) != 4:
        return None
    x0, y0, x1, y1 = coordinates
    if x1 <= x0 or y1 <= y0:
        return None
    return coordinates


def _prediction_sections(results: Iterable[Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for result in results:
        payload = _result_payload(result)
        boxes = payload.get("boxes") or []
        boxes = boxes.tolist() if hasattr(boxes, "tolist") else boxes
        for item in boxes:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            box = _as_box(item.get("coordinate"))
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            if label and box is not None and score >= _LAYOUT_THRESHOLD:
                sections.append({"label": label, "score": score, "box": box})
    return sections


def _auto_layout_needed(lines: list[dict[str, Any]]) -> bool:
    """Avoid loading layout detection for ordinary photos with a short sign."""
    return (
        len(lines) >= _AUTO_LAYOUT_MIN_LINES
        and sum(len(str(line.get("text") or "")) for line in lines) >= _AUTO_LAYOUT_MIN_CHARACTERS
    )


def _image_size(path: str) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return int(image.width), int(image.height)


def _predict(pipeline: Any, path: str) -> dict[str, Any]:
    width, height = _image_size(path)
    results = pipeline.predict(path)
    return {"width": width, "height": height, "lines": _prediction_lines(results), "sections": []}


def _prepare(runtime_root: Path, manifest_path: Path) -> int:
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    model_root = runtime_root / "models"
    model_root.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(model_root)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "true"
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")

    from paddleocr import LayoutDetection, PaddleOCR

    detection_name = str(manifest["models"]["detection"])
    recognition_name = str(manifest["models"]["recognition"])
    layout_name = str(manifest["models"]["layout"])
    # With no directory arguments PaddleOCR downloads official models into the
    # private cache above.  Only the two mobile models are enabled.
    pipeline = PaddleOCR(
        text_detection_model_name=detection_name,
        text_recognition_model_name=recognition_name,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
        device="cpu",
    )
    detection_dir = _model_directory(model_root, detection_name)
    recognition_dir = _model_directory(model_root, recognition_name)
    # Keep layout separate from the much heavier PP-Structure pipeline. This
    # model classifies regions while the existing mobile OCR models continue
    # to recognize text.
    layout_pipeline = LayoutDetection(
        model_name=layout_name,
        threshold=_LAYOUT_THRESHOLD,
        enable_mkldnn=False,
        device="cpu",
    )
    layout_dir = _model_directory(model_root, layout_name)

    # Exercise actual native inference before this runtime is eligible to
    # become active.  A blank image is enough: successful empty OCR proves that
    # model loading, preprocessing and both Paddle predictors work together.
    from PIL import Image

    health_image = runtime_root / ".health-check.png"
    Image.new("RGB", (640, 480), "white").save(health_image)
    try:
        list(pipeline.predict(str(health_image)))
        list(layout_pipeline.predict(str(health_image)))
    finally:
        health_image.unlink(missing_ok=True)

    runtime = {
        "schema_version": 2,
        "runtime_revision": int(manifest["runtime_revision"]),
        "python": ".venv/bin/python",
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "packages": {
            "paddlepaddle": importlib.metadata.version("paddlepaddle"),
            "paddleocr": importlib.metadata.version("paddleocr"),
        },
        "models": {
            "detection_name": detection_name,
            "detection_dir": str(detection_dir.relative_to(runtime_root)),
            "recognition_name": recognition_name,
            "recognition_dir": str(recognition_dir.relative_to(runtime_root)),
            "layout_name": layout_name,
            "layout_dir": str(layout_dir.relative_to(runtime_root)),
        },
    }
    target = runtime_root / "runtime.json"
    temporary = runtime_root / ".runtime.json.tmp"
    temporary.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return 0


def _serve(runtime_root: Path) -> int:
    config = _runtime_config(runtime_root)
    pipeline = _make_pipeline(runtime_root, config)
    layout_pipeline = None
    _emit({"event": "ready"})
    for raw_line in sys.stdin:
        request_id: Any = None
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise TypeError("request must be an object")
            request_id = request.get("id")
            path = request.get("path")
            if not isinstance(path, str) or not os.path.isfile(path):
                raise ValueError("path is not a local file")
            layout = request.get("layout", "auto")
            if layout not in (True, False, "auto"):
                raise ValueError("layout must be true, false, or 'auto'")
            result = _predict(pipeline, path)
            wants_layout = layout is True or (
                layout == "auto" and _auto_layout_needed(result["lines"])
            )
            if wants_layout:
                if layout_pipeline is None:
                    layout_pipeline = _make_layout_pipeline(runtime_root, config)
                result["sections"] = _prediction_sections(layout_pipeline.predict(path))
            _emit({"id": request_id, "result": result})
        except Exception as error:
            _emit({"id": request_id, "error": f"{type(error).__name__}: {error}"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--serve", action="store_true")
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    runtime_root = args.runtime_root.resolve()
    if args.prepare:
        if args.manifest is None:
            parser.error("--prepare requires --manifest")
        return _prepare(runtime_root, args.manifest.resolve())
    return _serve(runtime_root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PaddleOCR helper failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
