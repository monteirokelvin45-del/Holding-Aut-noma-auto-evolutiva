"""Backends de geração de vídeo e imagem com IA aberta (licença comercial).

Porque este módulo existe: o `FootageProvider` só sabia usar Pexels/Pixabay
(banco de imagens) ou gerar um fundo procedimental com ffmpeg. A spec pede
footage **gerado por IA**. Aqui fica a camada que fala com modelos abertos,
todos comercialmente utilizáveis e todos a **custo $0.00** (correm no seu
próprio hardware, dentro do `docker-compose`, ou ficam em `disabled`).

Backends suportados (por ordem de preferência configurável):

=========== ====================== =============== ============================
Backend     Repositório            Licença         Nota
=========== ====================== =============== ============================
``wan22``   Wan-Video/Wan2.2       Apache-2.0      melhor qualidade geral; 5B em 8GB
``longcat`` meituan-longcat/...    MIT             vídeos longos estáveis
``kandinsky``ai-forever/Kandinsky   MIT             tem áudio/lip-sync nativo
``ltx``     Lightricks/LTX-Video   Apache-2.0      comercial grátis < US$ 10M receita
``sdxl``    Stability-AI/...       Stability       imagem (SDXL / SD 3.5)
=========== ====================== =============== ============================

Nunca se chama um destes por HTTP sem declarar a licença — o departamento
jurídico (`legal_compliance.audit_media`) tem a última palavra e bloqueia
qualquer fonte fora da allowlist.

Contrato de configuração: cada backend é um endpoint HTTP compatível com
``POST {endpoint}`` recebendo ``{"prompt", "width", "height", "fps",
"duration_seconds", "seed"}`` e devolvendo ``{"video_path"| "video_b64" |
"image_path"| "image_b64"}``. Isto serve ComfyUI (com `/prompt`), A1111, ou um
wrapper FastAPI mínimo sobre diffusers — sem prender o repo a uma stack só.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.logger import get_logger

log = get_logger("squads.genai")

# Metadados de licença/imagem de cada backend. Usados na auditoria jurídica e
# devolvidos na API para que o Presidente saiba sempre o que gerou o quê.
BACKENDS: Dict[str, Dict[str, Any]] = {
    "wan22": {
        "label": "Wan 2.2 (Alibaba)",
        "repo": "https://github.com/Wan-Video/Wan2.2",
        "license": "apache-2.0",
        "kind": "video",
        "settings_attr": "wan22_endpoint",
        "note": "Apache-2.0 sem restrições de uso comercial; variante 5B em 8 GB.",
    },
    "longcat": {
        "label": "LongCat-Video (Meituan)",
        "repo": "https://github.com/meituan-longcat/LongCat-Video",
        "license": "mit",
        "kind": "video",
        "settings_attr": "longcat_endpoint",
        "note": "MIT; bom para vídeos longos e movimentos estáveis.",
    },
    "kandinsky": {
        "label": "Kandinsky (ai-forever)",
        "repo": "https://github.com/ai-forever/Kandinsky-6",
        "license": "mit",
        "kind": "video",
        "settings_attr": "kandinsky_endpoint",
        "note": "MIT com áudio/lip-sync nativo.",
    },
    "ltx": {
        "label": "LTX-Video (Lightricks)",
        "repo": "https://github.com/Lightricks/LTX-Video",
        "license": "ltx-commercial",
        "kind": "video",
        "settings_attr": "ltx_endpoint",
        "note": "Uso comercial gratuito até US$ 10M de receita anual.",
    },
    "sdxl": {
        "label": "Stable Diffusion XL / SD 3.5",
        "repo": "https://github.com/Stability-AI/generative-models",
        "license": "stability-commercial",
        "kind": "image",
        "settings_attr": "sdxl_endpoint",
        "note": "Imagem estática; licença comercial até US$ 1M de receita.",
    },
}

# Fontes de footage que o jurídico aceita sem mais perguntas.
GENERATED_SOURCES = {
    "wan22", "wan2.2", "longcat", "kandinsky", "ltx", "sdxl", "sd3.5", "sd35",
    "generated",
}


@dataclass
class GenerationResult:
    """Resultado de uma geração, com proveniência completa."""

    ok: bool
    backend: str = ""
    kind: str = "video"
    # ``None`` = nenhum ficheiro produzido. Nunca "" (ambíguo para o render).
    path: Optional[str] = None
    license: str = ""
    prompt: str = ""
    seed: int = 0
    latency_ms: int = 0
    reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "kind": self.kind,
            "path": self.path,
            "license": self.license,
            "prompt": self.prompt,
            "seed": self.seed,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
            "raw": self.raw,
            "created_at": self.created_at,
        }

    def as_footage(self, query: str) -> Dict[str, Any]:
        """Adapta ao formato que o `VideoFactorySquad.render` espera."""
        return {
            # ``source`` é o que o `legal_compliance.audit_media` inspecciona:
            # tem de ser uma origem da allowlist ("genai" -> "generated").
            "source": "generated" if self.ok else "unavailable",
            "backend": self.backend,
            "kind": self.kind,
            "path": self.path,
            "license": self.license,
            "url": "",
            "query": query,
            "prompt": self.prompt,
            "available": self.ok,
            "note": (
                f"Gerado por IA local ({self.backend}) em {self.latency_ms} ms."
                if self.ok else f"Indisponível: {self.reason}"
            ),
            "genai": self.to_dict(),
        }


class GenerativeVideoBackends:
    """Cliente dos backends de geração de vídeo/imagem por IA aberta."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ----------------------------------------------------------- configuração
    def endpoint(self, backend: str) -> str:
        meta = BACKENDS.get(backend)
        if meta is None:
            return ""
        return str(getattr(settings, meta["settings_attr"], "") or "").strip()

    def order(self) -> List[str]:
        """Ordem configurável (``GENAI_VIDEO_ORDER``), filtrada por tipo."""
        configured = [
            name.strip().lower()
            for name in (settings.genai_video_order or [])
            if name.strip()
        ]
        return [name for name in configured if name in BACKENDS]

    def status(self) -> Dict[str, Any]:
        """Estado de cada backend: pronto, sem endpoint ou desactivado."""
        services: Dict[str, Any] = {}
        for name, meta in BACKENDS.items():
            endpoint = self.endpoint(name)
            services[name] = {
                "label": meta["label"],
                "repo": meta["repo"],
                "license": meta["license"],
                "kind": meta["kind"],
                "endpoint": endpoint or None,
                "ready": bool(endpoint),
                "note": meta["note"],
            }
        ready = [name for name, data in services.items() if data["ready"]]
        return {
            "enabled": bool(settings.genai_video_enabled),
            "order": self.order(),
            "backends": services,
            "ready": ready,
            "active_source": ready[0] if ready else "generated",
            "policy": (
                "Só modelos abertos com licença comercial. CC-BY-NC bloqueado "
                "pelo departamento jurídico antes de qualquer render."
            ),
        }

    # ------------------------------------------------------------------ geração
    def _post(self, endpoint: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
        parsed = json.loads(body) if body.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("Resposta do backend não é um objecto JSON.")
        return parsed

    def _materialize(
        self,
        backend: str,
        parsed: Dict[str, Any],
        out_dir: Path,
        kind: str,
    ) -> Optional[str]:
        """Escreve o ficheiro devolvido (caminho local ou base64) no cofre."""
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".mp4" if kind == "video" else ".png"
        target = out_dir / f"{backend}_{utcnow_iso().replace(':', '')[:15]}{suffix}"

        for key in ("video_path", "image_path", "path", "file"):
            candidate = parsed.get(key)
            if candidate and Path(str(candidate)).is_file():
                source = Path(str(candidate))
                target.write_bytes(source.read_bytes())
                return str(target)

        for key in ("video_b64", "image_b64", "b64", "data"):
            encoded = parsed.get(key)
            if isinstance(encoded, str) and len(encoded) > 64:
                try:
                    target.write_bytes(base64.b64decode(encoded))
                    return str(target)
                except (ValueError, base64.binascii.Error):
                    continue

        for key in ("video_url", "image_url", "url"):
            url = parsed.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                try:
                    with urllib.request.urlopen(url, timeout=120) as response:
                        target.write_bytes(response.read())
                    return str(target)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    log.warning("Download do backend %s falhou: %s", backend, exc)
        return None

    def generate(
        self,
        prompt: str,
        *,
        out_dir: Path,
        kind: str = "video",
        duration_seconds: float = 6.0,
        seed: int = 0,
    ) -> GenerationResult:
        """Gera footage/imagem pelo primeiro backend pronto. Nunca lança.

        Sem endpoints configurados devolve ``ok=False`` com motivo explícito —
        o ``VideoFactorySquad`` continua com o fundo procedimental e declara-o.
        """
        def _skip(reason: str) -> GenerationResult:
            """Falha explícita: diz *o que* faltou, com a licença do 1.º da ordem."""
            head = self.order()[0] if self.order() else ""
            return GenerationResult(
                ok=False,
                backend=head,
                prompt=prompt,
                kind=kind,
                license=BACKENDS.get(head, {}).get("license", "unknown"),
                reason=reason,
            )

        if not settings.genai_video_enabled:
            return _skip("GENAI_VIDEO_ENABLED=false")

        candidates = [
            name for name in self.order()
            if self.endpoint(name) and BACKENDS[name]["kind"] in {kind, "video"}
        ]
        if not candidates:
            return _skip(
                "Nenhum backend de geração configurado "
                f"(ordem={self.order() or 'vazia'})."
            )

        import time

        errors: List[str] = []
        for backend in candidates:
            meta = BACKENDS[backend]
            endpoint = self.endpoint(backend)
            payload = {
                "prompt": prompt,
                "negative_prompt": (
                    "texto ilegível, marcas de água, logótipos, rostos de "
                    "pessoas reais, violência, conteúdo sexual"
                ),
                "width": 480 if kind == "video" else 1024,
                "height": 854 if kind == "video" else 1024,
                "fps": 16,
                "duration_seconds": max(2.0, min(10.0, duration_seconds)),
                "seed": seed,
                "num_inference_steps": 24,
            }
            started = time.monotonic()
            try:  # pragma: no cover - requer serviço de GPU a responder
                parsed = self._post(endpoint, payload, timeout=settings.genai_timeout_seconds)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                errors.append(f"{backend}: {exc}")
                log.warning("Backend %s falhou: %s", backend, exc)
                continue

            path = self._materialize(backend, parsed, out_dir, meta["kind"])
            latency = int((time.monotonic() - started) * 1000)
            if not path:
                errors.append(f"{backend}: resposta sem ficheiro")
                continue
            log.info(
                "Footage gerado por %s (%s) em %d ms -> %s",
                backend, meta["license"], latency, path,
            )
            return GenerationResult(
                ok=True,
                backend=backend,
                kind=meta["kind"],
                path=path,
                license=meta["license"],
                prompt=prompt,
                seed=int(payload["seed"]),
                latency_ms=latency,
                raw={k: v for k, v in parsed.items() if k not in {
                    "video_b64", "image_b64", "b64", "data"}},
            )

        return _skip("; ".join(errors)[:400] or "sem endpoint configurado")


def build_visual_prompt(topic: str, niche: str = "") -> str:
    """Constrói o prompt visual do footage a partir do tema do vídeo.

    Deliberadamente sem pessoas identificáveis e sem logótipos: reduz risco de
    direitos de imagem e de marcas no render.
    """
    subject = (topic or niche or "negócio local").strip()[:160]
    return (
        f"Plano cinematográfico vertical 9:16 de {subject}, ambiente de "
        "comércio em Moçambique, luz natural suave de fim de tarde, cores "
        "quentes, profundidade de campo rasa, movimento lento de câmara, "
        "sem texto, sem logótipos, sem pessoas identificáveis, fotorrealista."
    )


# Instância partilhada.
genai_backends = GenerativeVideoBackends()
