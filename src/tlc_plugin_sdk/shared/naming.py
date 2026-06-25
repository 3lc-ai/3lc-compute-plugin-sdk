# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Random name generator for runs, jobs, and other entities.

Produces memorable names from data science and computer vision vocabulary.
"""

from __future__ import annotations

import random

_ADJECTIVES = [
    "robust",
    "sparse",
    "dense",
    "latent",
    "frozen",
    "residual",
    "pooled",
    "stacked",
    "gated",
    "masked",
    "dilated",
    "pretrained",
    "finetuned",
    "attentive",
    "contrastive",
    "recurrent",
    "augmented",
    "calibrated",
    "distilled",
    "quantized",
    "pruned",
    "anchored",
    "focal",
    "semantic",
    "adaptive",
    "causal",
    "spectral",
    "variational",
    "embedded",
    "strided",
]

_NOUNS = [
    "gradient",
    "tensor",
    "kernel",
    "encoder",
    "decoder",
    "backbone",
    "embedding",
    "bottleneck",
    "convolution",
    "attention",
    "dropout",
    "batchnorm",
    "activation",
    "classifier",
    "regressor",
    "detector",
    "segmenter",
    "feature",
    "logit",
    "softmax",
    "perceptron",
    "transformer",
    "diffusion",
    "manifold",
    "centroid",
    "heatmap",
    "mosaic",
    "anchor",
    "pipeline",
    "checkpoint",
]


def generate_name() -> str:
    """Generate a memorable name like ``robust-gradient-42``."""
    adj = random.choice(_ADJECTIVES)
    noun = random.choice(_NOUNS)
    num = random.randint(10, 99)
    return f"{adj}-{noun}-{num}"
