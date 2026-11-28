"""Servidor de geração de vídeo/imagem por IA aberta (compatível com a GPU).

Este serviço é o lado *fornecedor* do contrato de `ai_video_backends.py`::

    POST /generate
    {
        "prompt": "...", "negative_prompt": "...",
        "width": 768, "height": 1344, "fps": 24,
        "duration_seconds": 4.0, "seed": 42,
        "num_inference_steps": 30
    }
    -> {"video_b64": "..."} | {"video_path": "/models/out/xxx.mp4"}
       (ou {"image_b64": ...} quando ``kind="image"``)

O servidor não embute pesos. Na primeira chamada resolve o backend pedido:

* **Wan 2.2** (Apache-2.0) — difusão de vídeo texto→vídeo, 5B em 8 GB VRAM;
* **LTX-Video** (Apache-2.0) — rápido, bom para 4–8 s a 24 fps;
* **LongCat-Video** (MIT) — vídeo longo;
* **Kandinsky 6.0** (MIT) — imagem/vídeo com estética forte;
* **SDXL / SD 3.5** (Stability community license) — imagens estáticas.

Estratégia de execução, por ordem, sem inventar capacidades:

1. ``diffusers`` instalado + GPU CUDA -> inferência local real (`_diffusers`);
2. ComfyUI local acessível em ``COMFYUI_URL`` -> API ``/prompt`` (`_comfyui`);
3. nenhum dos dois -> HTTP 503 com o motivo exacto, e o orquestrador cai para
   o banco de imagens (Pexels/Pixabay) como já faz hoje.

Como em todo o projecto, nada aqui gasta dinheiro: só modelos abertos e
hardware próprio. Licenças verificadas em `docs/stack_audit.md`.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

try:  # FastAPI é dependência de base do projecto
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - ambiente sem FastAPI
    raise SystemExit(
        "genai_server precisa de fastapi/pydantic: pip install fastapi uvicorn pydantic"
    ) from exc

MODELS_DIR = Path(os.environ.get("GENAI_MODELS_DIR", "/models"))
OUT_DIR = MODELS_DIR / "out"
DEFAULT_BACKEND = os.environ.get("GENAI_DEFAULT_BACKEND", "wan22")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "").rstrip("/")

#: Repositórios HuggingFace de cada backend — pesos abertos, sem chaves pagas.
MODEL_IDS: Dict[str, Dict[str, str]] = {
    "wan22": {
        "repo": "Wan-AI/Wan2.2-T2V-A14B-Diffusers",
        "fallback_repo": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
        "license": "apache-2.0",
        "kind": "video",
    },
    "ltx": {
        "repo": "Lightricks/LTX-Video",
        "fallback_repo": "Lightricks/LTX-Video-0.9.5",
        "license": "apache-2.0",
        "kind": "video",
    },
    "longcat": {
        "repo": "meituan-longcat/LongCat-Video",
        "fallback_repo": "meituan-longcat/LongCat-Video",
        "license": "mit",
        "kind": "video",
    },
    "kandinsky": {
        "repo": "kandinsky-community/kandinsky-3",
        "fallback_repo": "kandinsky-community/kandinsky-2-2-decoder",
        "license": "mit",
        "kind": "image",
    },
    "sdxl": {
        "repo": "stabilityai/stable-diffusion-xl-base-1.0",
        "fallback_repo": "stabilityai/sdxl-turbo",
        "license": "stability-community",
        "kind": "image",
    },
    "sd35": {
        "repo": "stabilityai/stable-diffusion-3.5-medium",
        "fallback_repo": "stabilityai/stable-diffusion-3.5-large-turbo",
        "license": "stability-community",
        "kind": "image",
    },
}

app = FastAPI(title="Holding — Gerador de Média por IA Aberta", version="1.0.0")


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    negative_prompt: str = "texto, legendas, logótipos, marcas de água, pessoas identificáveis"
    width: int = Field(default=768, ge=256, le=1920)
    height: int = Field(default=1344, ge=256, le=1920)
    fps: int = Field(default=24, ge=8, le=60)
    duration_seconds: float = Field(default=4.0, gt=0.0, le=30.0)
    seed: Optional[int] = None
    num_inference_steps: int = Field(default=30, ge=4, le=80)
    backend: Optional[str] = None
    kind: Optional[str] = None


def _resolve_backend(name: Optional[str]) -> tuple[str, Dict[str, str]]:
    key = (name or DEFAULT_BACKEND).strip().lower()
    if key not in MODEL_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"backend desconhecido: {key}; válidos: {sorted(MODEL_IDS)}",
        )
    return key, MODEL_IDS[key]


def _torch_status() -> Dict[str, Any]:
    try:
        import torch  # type: ignore
    except ImportError:
        return {"installed": False, "cuda": False, "device": "cpu"}
    cuda = bool(getattr(torch, "cuda", None) and torch.cuda.is_available())
    return {
        "installed": True,
        "cuda": cuda,
        "device": "cuda" if cuda else "cpu",
        "vram_gb": (
            round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)
            if cuda else 0.0
        ),
    }


def _write_out(data: bytes, suffix: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    return path


def _diffusers(req: GenerateRequest, model: Dict[str, str], key: str) -> Dict[str, Any]:
    """Inferência real com ``diffusers`` — devolve caminho + metadados."""
    import torch  # type: ignore
    from diffusers import DiffusionPipeline  # type: ignore

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    loaded_from = model["repo"]
    try:
        pipe = DiffusionPipeline.from_pretrained(model["repo"], torch_dtype=dtype)
    except Exception:  # repo grande demais para a VRAM -> fallback menor
        loaded_from = model["fallback_repo"]
        pipe = DiffusionPipeline.from_pretrained(
            model["fallback_repo"], torch_dtype=dtype
        )
    pipe = pipe.to(device)
    if hasattr(pipe, "enable_attention_slicing"):
        pipe.enable_attention_slicing()

    generator = torch.Generator(device=device)
    if req.seed is not None:
        generator.manual_seed(int(req.seed))

    is_image = (req.kind or model["kind"]) == "image"
    common = {
        "prompt": req.prompt,
        "negative_prompt": req.negative_prompt,
        "num_inference_steps": req.num_inference_steps,
        "generator": generator,
    }
    if is_image:
        result = pipe(
            **common, width=req.width, height=req.height
        )
        image = result.images[0]
        path = _write_out(b"", ".png")
        image.save(path)
        return {"image_path": str(path), "model": loaded_from}

    frames = max(8, int(req.duration_seconds * req.fps))
    kwargs: Dict[str, Any] = {
        **common,
        "width": req.width,
        "height": req.height,
        "num_frames": frames,
    }
    result = pipe(**kwargs)
    frames_list = result.frames[0] if isinstance(result.frames, list) else result.frames
    path = _write_out(b"", ".gif")
    try:  # exportação vídeo real quando imageio-ffmpeg está disponível
        import numpy as np  # type: ignore
        import imageio.v2 as imageio  # type: ignore

        mp4 = path.with_suffix(".mp4")
        writer = imageio.get_writer(str(mp4), fps=req.fps, codec="libx264")
        for frame in frames_list:
            writer.append_data(np.asarray(frame))
        writer.close()
        path.unlink(missing_ok=True)
        return {"video_path": str(mp4), "model": loaded_from}
    except Exception:
        frames_list[0].save(
            path, save_all=True, append_images=list(frames_list[1:]), fps=req.fps
        )
        return {"video_path": str(path), "model": loaded_from, "format": "gif"}


def _comfyui(req: GenerateRequest, model: Dict[str, str], key: str) -> Dict[str, Any]:
    """Delega a um ComfyUI local: o grafo é o template T2V por omissão."""
    import json
    import urllib.request

    graph = {
        "prompt": req.prompt,
        "negative_prompt": req.negative_prompt,
        "width": req.width,
        "height": req.height,
        "fps": req.fps,
        "seconds": req.duration_seconds,
        "seed": req.seed or int(time.time()) % 100000,
        "steps": req.num_inference_steps,
        "model": model["repo"],
        "backend": key,
    }
    request = urllib.request.Request(
        COMFYUI_URL + "/prompt",
        data=json.dumps({"prompt": graph}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.loads(response.read().decode("utf-8"))
    prompt_id = body.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI recusou o pedido: {body}")
    return {"comfyui_prompt_id": prompt_id, "poll": f"/history/{prompt_id}"}


@app.get("/health")
def health() -> Dict[str, Any]:
    torch_state = _torch_status()
    comfy = bool(COMFYUI_URL)
    return {
        "ok": torch_state["cuda"] or comfy,
        "default_backend": DEFAULT_BACKEND,
        "backends": sorted(MODEL_IDS),
        "torch": torch_state,
        "comfyui": {"configured": comfy, "url": COMFYUI_URL or None},
        "models_dir": str(MODELS_DIR),
    }


@app.get("/backends")
def backends() -> Dict[str, Any]:
    return {
        "default": DEFAULT_BACKEND,
        "backends": MODEL_IDS,
        "note": "pesos descarregados de HuggingFace na primeira execução; $0.00",
    }


@app.post("/generate")
def generate(req: GenerateRequest) -> Dict[str, Any]:
    key, model = _resolve_backend(req.backend)
    started = time.time()
    errors = []

    torch_state = _torch_status()
    if torch_state["installed"]:
        try:
            payload = _diffusers(req, model, key)
            return {
                "ok": True,
                "backend": key,
                "license": model["license"],
                "seed": req.seed,
                "latency_ms": round((time.time() - started) * 1000, 1),
                **payload,
            }
        except Exception as exc:  # VRAM, OOM, pesos em falta
            errors.append(f"diffusers: {exc}")

    if COMFYUI_URL:
        try:
            payload = _comfyui(req, model, key)
            return {
                "ok": True,
                "backend": key,
                "license": model["license"],
                "seed": req.seed,
                "latency_ms": round((time.time() - started) * 1000, 1),
                **payload,
            }
        except Exception as exc:
            errors.append(f"comfyui: {exc}")

    raise HTTPException(
        status_code=503,
        detail={
            "ok": False,
            "backend": key,
            "reason": "sem GPU/diffusers e sem ComfyUI — usar banco de imagens",
            "errors": errors,
        },
    )
