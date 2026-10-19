"""Immutable runtime contract shared by installer and deep doctor."""

from __future__ import annotations


RUNTIME_SCHEMA_VERSION = 1
RUNTIME_VERSION = "0.1.0"
RUNTIME_NAME = "ebook-to-kindle"
MAX_PERSISTENT_BYTES = 6 * 1024**3

CORE_COMPONENT = {
    "calibre": {
        "version": "9.13.0",
        "sha256": "001be6ed70d8acfd793fa7d8c95ea50045abedaf0e756b20e564275a0b0a7667",
    },
    "epubcheck": {
        "version": "5.3.0",
        "sha256": "6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5",
    },
    "temurin_jre": {
        "version": "21.0.12+8",
        "sha256": "36bb71d6fa5184e12a6483e7662783c2cbd383f5dca8034140f0a84dd5aa797d",
    },
}

PDF_VERSIONS = {
    "python": "3.12.13",
    "docling": "2.119.0",
    "ocrmac": "1.0.1",
    "pypdfium2": "5.12.1",
}

PDF_PYTHON_ASSET = {
    "version": "3.12.13+20260807",
    "sha256": "25baa97c65b3f0aa90e21131b4f9e80aef8899e8144006db8a9d2c1ab9e807e3",
}

MODEL_HASHES = {
    "layout_heron": "00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c",
    "layout_heron_onnx": "59c81a3a2923042d85034ffc487f8f47e4854117e879aef89b2b9f728fb4922a",
    "tableformer_fast": "3119563aab5a7c96fda4d621119b63fd8806272b86c30936d15507616422f718",
    "tableformer_accurate": "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9",
}

MODEL_FILES = {
    "docling-project--docling-layout-heron/model.safetensors": MODEL_HASHES["layout_heron"],
    "docling-project--docling-layout-heron-onnx/model.onnx": MODEL_HASHES["layout_heron_onnx"],
    "docling-project--docling-models/model_artifacts/tableformer/fast/tableformer_fast.safetensors": MODEL_HASHES[
        "tableformer_fast"
    ],
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors": MODEL_HASHES[
        "tableformer_accurate"
    ],
}
