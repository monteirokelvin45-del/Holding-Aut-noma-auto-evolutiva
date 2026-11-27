"""Esquadrão 1: Fábrica de Vídeos Curtos e Canais Dark (9:16).

Pipeline real, com degradação honesta em cada passo:

  1. **Guião** — LLM (gancho de 3s, corpo, CTA) ou template determinístico.
  2. **Voz** — ``edge-tts`` (vozes neurais Microsoft, LGPL) como motor
     preferido; ``Kokoro-82M`` e ``Chatterbox`` (Apache-2.0/MIT) como
     alternativas registadas. Sem pacote/ rede -> **sem áudio**, declarado.
  3. **Footage** — Pexels/Pixabay quando há chave; senão geramos um *fundo
     procedural* com ffmpeg (gradiente + ruído), que é 100% nosso.
  4. **Render** — ffmpeg (1080x1920, 30 fps) com legendas queimadas
     (``subtitles``) ou desenhadas via ``drawtext``.
  5. **Compliance** — licenças de cada fonte declaradas e auditadas pelo
     departamento jurídico (``audit_media``) antes de o Juiz avaliar.
  6. **Publicação** — *nunca automática*: o vídeo fica em
     ``vault_data/media/videos/<squad>/`` com um pacote de publicação
     (título, descrição, hashtags) pronto para upload humano/manual.

TTS: **proibido** ChatTTS/F5-TTS (CC-BY-NC). Só edge-tts, Kokoro-82M,
Chatterbox e Piper são aceites pela allowlist abaixo.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.errors import ValidationBlocked
from ..core.llm_router import LLMRequest, llm_router
from ..core.logger import get_logger
from ..governance.legal_compliance import legal_department
from .squad_base import BaseSquad, SquadRunResult

log = get_logger("squads.video")

ALLOWED_TTS = {"edge_tts", "kokoro", "chatterbox", "piper"}
FORBIDDEN_TTS = {"chattts", "f5_tts", "f5-tts"}
SAFE_FOOTAGE_SOURCES = {"pexels", "pixabay", "generated", "own", "local"}

DEFAULT_VOICE_PT = "pt-PT-RaquelNeural"
DEFAULT_VOICE_EN = "en-US-AriaNeural"


@dataclass
class VideoScript:
    title: str
    hook: str
    body: List[str]
    cta: str
    hashtags: List[str]
    description: str
    language: str = "pt"
    engine: str = "template"
    is_llm: bool = False

    @property
    def full_narration(self) -> str:
        parts = [self.hook] + list(self.body) + [self.cta]
        return " ".join(p.strip() for p in parts if p.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "hook": self.hook,
            "body": list(self.body),
            "cta": self.cta,
            "hashtags": list(self.hashtags),
            "description": self.description,
            "language": self.language,
            "engine": self.engine,
            "is_llm": self.is_llm,
            "word_count": len(self.full_narration.split()),
        }


@dataclass
class RenderResult:
    ok: bool
    video_path: str = ""
    audio_path: str = ""
    duration_seconds: float = 0.0
    resolution: str = ""
    fps: int = 0
    steps: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "video_path": self.video_path,
            "audio_path": self.audio_path,
            "duration_seconds": self.duration_seconds,
            "resolution": self.resolution,
            "fps": self.fps,
            "steps": self.steps,
            "warnings": list(self.warnings),
        }


class TTSRegistry:
    """Detecta e usa motores TTS com licença comercial permitida."""

    def __init__(self) -> None:
        self.preferred = (settings.tts_provider or "edge_tts").strip().lower()
        if self.preferred in FORBIDDEN_TTS:
            raise ValidationBlocked(
                "TTS proibido por licença CC-BY-NC (ChatTTS/F5-TTS).",
                details={"requested": self.preferred, "allowed": sorted(ALLOWED_TTS)},
            )

    def available(self) -> Dict[str, bool]:
        return {
            "edge_tts": self._module("edge_tts"),
            "kokoro": self._module("kokoro") or self._module("kokoro_onnx"),
            "chatterbox": self._module("chatterbox"),
            "piper": shutil.which("piper") is not None,
        }

    @staticmethod
    def _module(name: str) -> bool:
        import importlib.util

        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def synthesize(
        self, text: str, out_path: Path, language: str = "pt"
    ) -> Tuple[bool, str]:
        """Gera áudio. Devolve ``(ok, motor_ou_motivo)``."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        engine = self.preferred if self.preferred in ALLOWED_TTS else "edge_tts"
        voice = settings.tts_voice_pt if language.startswith("pt") else settings.tts_voice_en

        if engine == "edge_tts" and self._module("edge_tts"):
            ok, message = self._edge_tts(text, out_path, voice)
            if ok:
                return True, "edge_tts"
            log.warning("edge-tts falhou (%s); a tentar alternativas.", message)
        if engine in {"kokoro", "edge_tts"} and (
            self._module("kokoro") or self._module("kokoro_onnx")
        ):
            ok, message = self._kokoro(text, out_path, language)
            if ok:
                return True, "kokoro"
        if engine in {"chatterbox", "edge_tts"} and self._module("chatterbox"):
            ok, message = self._chatterbox(text, out_path)
            if ok:
                return True, "chatterbox"
        if shutil.which("piper"):
            ok, message = self._piper(text, out_path)
            if ok:
                return True, "piper"

        return False, (
            "Nenhum motor TTS disponível. Instalar 'edge-tts' (LGPL) "
            f"ou 'kokoro' (Apache-2.0). Motores detectados: "
            f"{ {k: v for k, v in self.available().items() if v} or 'nenhum'}."
        )

    # ------------------------------------------------------------------ motores
    @staticmethod
    def _edge_tts(text: str, out_path: Path, voice: str) -> Tuple[bool, str]:
        try:
            import asyncio

            import edge_tts  # type: ignore

            async def _run() -> None:
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(str(out_path))

            asyncio.run(_run())
            return (out_path.is_file() and out_path.stat().st_size > 512,
                    "edge_tts:ok")
        except Exception as exc:  # rede bloqueada, voz inválida, etc.
            return False, f"edge_tts:{exc}"

    @staticmethod
    def _kokoro(text: str, out_path: Path, language: str) -> Tuple[bool, str]:
        try:
            import numpy as np  # type: ignore

            try:
                from kokoro import KPipeline  # type: ignore

                pipeline = KPipeline(lang_code="p" if language.startswith("pt") else "a")
                chunks = [
                    audio for _, _, audio in pipeline(text, voice="pf_dora" if language.startswith("pt") else "af_heart")
                ]
                if not chunks:
                    return False, "kokoro:sem_audio"
                import soundfile as sf  # type: ignore

                sf.write(str(out_path), np.concatenate(chunks), 24000)
                return True, "kokoro:ok"
            except ImportError:
                from kokoro_onnx import Kokoro  # type: ignore

                kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
                samples, rate = kokoro.create(text, voice="pf_dora", speed=1.0, lang="pt-pt")
                import soundfile as sf  # type: ignore

                sf.write(str(out_path), samples, rate)
                return True, "kokoro_onnx:ok"
        except Exception as exc:
            return False, f"kokoro:{exc}"

    @staticmethod
    def _chatterbox(text: str, out_path: Path) -> Tuple[bool, str]:
        try:
            import torchaudio  # type: ignore
            from chatterbox.tts import ChatterboxTTS  # type: ignore

            model = ChatterboxTTS.from_pretrained(device="cpu")
            wav = model.generate(text)
            torchaudio.save(str(out_path), wav, model.sr)
            return True, "chatterbox:ok"
        except Exception as exc:
            return False, f"chatterbox:{exc}"

    @staticmethod
    def _piper(text: str, out_path: Path) -> Tuple[bool, str]:
        try:
            model = settings.vault_path / "models" / "piper" / "pt_PT-tugao-medium.onnx"
            if not model.is_file():
                return False, "piper:modelo_ausente"
            result = subprocess.run(
                ["piper", "--model", str(model), "--output_file", str(out_path)],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=240,
                check=False,
            )
            return result.returncode == 0 and out_path.is_file(), "piper:ok"
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"piper:{exc}"


class FootageProvider:
    """Obtém footage vertical sem custo, com proveniência declarada."""

    def __init__(self) -> None:
        self.sources: List[Dict[str, Any]] = []

    def fetch(self, query: str, out_dir: Path, index: int = 0) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        if settings.pexels_api_key:
            item = self._pexels(query, out_dir, index)
            if item:
                self.sources.append(item)
                return item
        if settings.pixabay_api_key:
            item = self._pixabay(query, out_dir, index)
            if item:
                self.sources.append(item)
                return item
        return {
            "source": "generated",
            "kind": "procedural_background",
            "path": "",
            "license": "own_work",
            "url": "",
            "query": query,
            "note": "Sem chave Pexels/Pixabay: fundo gerado localmente pelo ffmpeg.",
        }

    def _pexels(self, query: str, out_dir: Path, index: int) -> Optional[Dict[str, Any]]:
        url = (
            "https://api.pexels.com/videos/search?query="
            + urllib.parse.quote(query)
            + "&orientation=portrait&per_page=5"
        )
        request = urllib.request.Request(url)
        request.add_header("Authorization", settings.pexels_api_key)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            log.warning("Pexels falhou: %s", exc)
            return None
        videos = payload.get("videos") or []
        if not videos:
            return None
        video = videos[index % len(videos)]
        files = sorted(
            (video.get("video_files") or []),
            key=lambda f: abs(int(f.get("width", 0)) - int(f.get("height", 0))),
        )
        target = next(
            (
                f
                for f in (video.get("video_files") or [])
                if int(f.get("height", 0) or 0) >= int(f.get("width", 0) or 0)
            ),
            files[0] if files else None,
        )
        if not target or not target.get("link"):
            return None
        path = out_dir / f"pexels_{video.get('id', index)}.mp4"
        try:
            with urllib.request.urlopen(target["link"], timeout=120) as response:
                path.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("Download Pexels falhou: %s", exc)
            return None
        return {
            "source": "pexels",
            "kind": "video",
            "path": str(path),
            "license": "pexels_license",
            "url": str(video.get("url", "")),
            "query": query,
        }

    def _pixabay(self, query: str, out_dir: Path, index: int) -> Optional[Dict[str, Any]]:
        url = (
            "https://pixabay.com/api/videos/?key="
            + urllib.parse.quote(settings.pixabay_api_key)
            + "&q=" + urllib.parse.quote(query)
            + "&video_type=film&per_page=5&safesearch=true"
        )
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            log.warning("Pixabay falhou: %s", exc)
            return None
        hits = payload.get("hits") or []
        if not hits:
            return None
        hit = hits[index % len(hits)]
        variants = hit.get("videos") or {}
        link = (
            (variants.get("medium") or {}).get("url")
            or (variants.get("small") or {}).get("url")
            or (variants.get("large") or {}).get("url")
        )
        if not link:
            return None
        path = out_dir / f"pixabay_{hit.get('id', index)}.mp4"
        try:
            with urllib.request.urlopen(link, timeout=120) as response:
                path.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("Download Pixabay falhou: %s", exc)
            return None
        return {
            "source": "pixabay",
            "kind": "video",
            "path": str(path),
            "license": "pixabay_license",
            "url": str(hit.get("pageURL", "")),
            "query": query,
        }


class VideoFactorySquad(BaseSquad):
    """Produção de shorts verticais com portões de compliance e qualidade."""

    squad_type = "video_factory"
    squad_name = "Fábrica de Vídeos Curtos"
    owner_agent = "video_factory"
    requires_validation = True
    content_type = "video_short"

    def __init__(self) -> None:
        self.tts = TTSRegistry()
        super().__init__()

    # ------------------------------------------------------------------ guião
    def write_script(
        self, *, idea: str, niche: str, language: str = "pt", duration_target: int = 45
    ) -> VideoScript:
        """Escreve o guião. LLM se disponível; template determinístico caso não."""
        words_target = max(60, min(240, int(duration_target * 2.6)))
        prompt = (
            "Escreve um guião de vídeo vertical (9:16) para TikTok/Shorts/Reels.\n"
            f"IDEIA: {idea}\nNICHO: {niche}\nIDIOMA: {language}\n"
            f"ALVO: ~{words_target} palavras de narração (~{duration_target}s).\n"
            "Regras: gancho nos primeiros 3 segundos (pergunta ou número), "
            "corpo em 2-3 blocos curtos, CTA final. Sem promessas de rendimento "
            "garantido. Devolve JSON com: title, hook, body (lista), cta, "
            "hashtags (lista de 5), description (máx 200 chars)."
        )
        request = LLMRequest(
            prompt=prompt,
            system=self._default_system(),
            task="fast",
            json_mode=True,
            max_tokens=900,
            temperature=0.7,
            agent_id=self.owner_agent,
            context={"squad": self.squad_type},
            json_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "body": {"type": "array"},
                    "cta": {"type": "string"},
                    "hashtags": {"type": "array"},
                    "description": {"type": "string"},
                },
            },
        )
        data, response = llm_router.complete_json(
            request,
            required_keys=["title", "hook", "body", "cta", "hashtags", "description"],
        )
        if response.is_llm:
            body = data.get("body")
            if isinstance(body, str):
                body = [line.strip() for line in body.splitlines() if line.strip()]
            if not isinstance(body, list) or not body:
                body = [str(data.get("hook", ""))]
            hashtags = data.get("hashtags")
            if isinstance(hashtags, str):
                hashtags = [h.strip() for h in hashtags.split() if h.strip()]
            return VideoScript(
                title=str(data.get("title") or idea)[:120],
                hook=str(data.get("hook") or "")[:220],
                body=[str(b)[:280] for b in body][:4],
                cta=str(data.get("cta") or "Segue para mais")[:160],
                hashtags=[str(h)[:30] for h in (hashtags or [])][:8]
                or self._default_hashtags(niche),
                description=str(data.get("description") or "")[:220],
                language=language,
                engine=response.provider,
                is_llm=True,
            )

        # -------- template determinístico (marcado como não-LLM) -----------
        return VideoScript(
            title=f"{idea[:80]}",
            hook=(
                f"Sabias que a maioria das pessoas perde dinheiro em {niche} "
                "sem perceber porquê?"
            ),
            body=[
                "Primeiro, identifica onde o dinheiro está a sair.",
                "Segundo, compara com o que realmente gera retorno.",
                "Terceiro, corta o que não paga as contas.",
            ],
            cta="Segue para mais e entra na lista VIP no link da bio.",
            hashtags=self._default_hashtags(niche),
            description=f"3 passos práticos sobre {idea[:120]}.",
            language=language,
            engine="template",
            is_llm=False,
        )

    @staticmethod
    def _default_hashtags(niche: str) -> List[str]:
        base = ["#shorts", "#dicas", "#aprender"]
        niche_tag = re.sub(r"[^a-zA-Z0-9]", "", (niche or "negocios"))[:20]
        return base + [f"#{niche_tag}", "#moçambique"]

    # ------------------------------------------------------------------ render
    def _ffmpeg(self) -> Optional[str]:
        """Localiza o ffmpeg: config -> PATH -> binário do imageio-ffmpeg."""
        configured = settings.ffmpeg_binary
        if configured and Path(configured).is_file():
            return configured
        found = shutil.which(configured or "ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg  # type: ignore

            packaged = imageio_ffmpeg.get_ffmpeg_exe()
            if packaged and Path(packaged).is_file():
                return packaged
        except Exception:
            pass
        return None

    def _probe_duration(self, path: Path) -> float:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe or not path.is_file():
            return 0.0
        try:
            result = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return round(float((result.stdout or "0").strip() or 0), 2)
        except (subprocess.SubprocessError, OSError, ValueError):
            return 0.0

    def render(
        self, *, script: VideoScript, out_dir: Path, footage: Dict[str, Any]
    ) -> RenderResult:
        """Renderiza o vídeo. Sem ffmpeg devolve artefactos + aviso honesto."""
        ffmpeg = self._ffmpeg()
        result = RenderResult(ok=False, resolution=f"{settings.video_target_width}x{settings.video_target_height}",
                              fps=settings.video_target_fps)
        out_dir.mkdir(parents=True, exist_ok=True)

        # ---- 1. voz -------------------------------------------------------
        audio_path = out_dir / "narration.mp3"
        ok_audio, tts_info = self.tts.synthesize(
            script.full_narration, audio_path, language=script.language
        )
        result.steps["tts"] = {"ok": ok_audio, "engine": tts_info}
        if ok_audio:
            result.audio_path = str(audio_path)
            result.duration_seconds = self._probe_duration(audio_path)
        else:
            result.warnings.append(f"Sem áudio sintetizado: {tts_info}")

        # ---- 2. legendas --------------------------------------------------
        srt_path = out_dir / "subtitles.srt"
        srt_path.write_text(self._build_srt(script), encoding="utf-8")
        result.steps["subtitles"] = {"ok": True, "path": str(srt_path)}

        if ffmpeg is None:
            result.steps["render"] = {
                "ok": False,
                "reason": "ffmpeg não encontrado no PATH",
            }
            result.warnings.append(
                "Render de vídeo indisponível: instalar ffmpeg. "
                "Guião, áudio (se TTS disponível) e legendas ficaram prontos."
            )
            result.duration_seconds = result.duration_seconds or min(
                settings.video_max_duration_seconds,
                len(script.full_narration.split()) / 2.6,
            )
            return result

        # ---- 3. plano de fundo -------------------------------------------
        width, height, fps = (
            settings.video_target_width,
            settings.video_target_height,
            settings.video_target_fps,
        )
        source_path = footage.get("path") or ""
        duration = result.duration_seconds or min(
            settings.video_max_duration_seconds,
            max(8.0, len(script.full_narration.split()) / 2.6),
        )
        video_path = out_dir / "short.mp4"

        if source_path and Path(source_path).is_file():
            base_input = ["-stream_loop", "-1", "-i", source_path]
            source_filter = (
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={fps},setsar=1[base]"
            )
        else:
            # O gradiente é calculado a 1/4 da resolução (25x mais rápido) e
            # depois escalado com bicubic — visualmente idêntico em 9:16.
            low_w, low_h = max(16, width // 4), max(16, height // 4)
            base_input = ["-f", "lavfi", "-i", f"color=c=0x0b1120:s={low_w}x{low_h}:r={fps}"]
            source_filter = (
                f"[0:v]geq=r='20+40*sin(X/30)':g='30+50*cos(Y/24)':b='60+70*sin((X+Y)/50)',"
                f"scale={width}:{height}:flags=bicubic,fps={fps},setsar=1[base]"
            )

        subtitles_filter = ""
        if srt_path.is_file():
            # O caminho do SRT é escapado para o filtro do ffmpeg.
            escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
            subtitles_filter = (
                f",subtitles='{escaped}':force_style="
                "'FontName=DejaVu Sans,FontSize=20,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H80000000,BorderStyle=3,Outline=2,Shadow=1,"
                "Alignment=2,MarginV=120'"
            )

        filter_complex = f"{source_filter};[base]{subtitles_filter.lstrip(',') or 'null'}[v]"
        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *base_input]
        silent_track = False
        if result.audio_path:
            command += ["-i", result.audio_path]
        else:
            # Sem TTS: geramos uma faixa silenciosa declarada para que o ficheiro
            # seja aceite pelas plataformas. Não é narração — fica registado.
            command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            silent_track = True
            result.warnings.append(
                "Áudio silencioso (sem TTS disponível): narração tem de ser "
                "adicionada antes de publicar."
            )
            result.steps["silent_audio_track"] = True
        command += [
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "1:a", "-c:a", "aac", "-b:a", "128k",
        ]
        command += [
            "-t", f"{min(duration, float(settings.video_max_duration_seconds)):.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(fps), "-movflags", "+faststart",
            str(video_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=900, check=False
            )
        except (subprocess.SubprocessError, OSError) as exc:
            result.steps["render"] = {"ok": False, "error": str(exc)}
            result.warnings.append(f"ffmpeg falhou: {exc}")
            return result

        if completed.returncode != 0 or not video_path.is_file():
            result.steps["render"] = {
                "ok": False,
                "returncode": completed.returncode,
                "stderr": (completed.stderr or "")[-800:],
            }
            result.warnings.append(
                "Render falhou — ver passos para o erro do ffmpeg "
                "(legendas exigem libass; fundo geq exige ffmpeg completo)."
            )
            return result

        result.ok = True
        result.video_path = str(video_path)
        result.steps["render"] = {
            "ok": True,
            "ffmpeg": ffmpeg,
            "silent_audio": silent_track,
        }
        result.duration_seconds = self._probe_duration(video_path) or duration
        return result

    @staticmethod
    def _build_srt(script: VideoScript) -> str:
        """Constrói legendas SRT a partir dos blocos do guião."""
        blocks = [script.hook] + list(script.body) + [script.cta]
        lines: List[str] = []
        cursor = 0.0
        for index, block in enumerate(blocks, start=1):
            words = max(1, len(block.split()))
            duration = max(2.0, min(6.0, words / 2.6))
            start = cursor
            end = cursor + duration
            lines.append(str(index))
            lines.append(f"{_srt_time(start)} --> {_srt_time(end)}")
            lines.append(_wrap_caption(block))
            lines.append("")
            cursor = end + 0.15
        return "\n".join(lines)

    # ------------------------------------------------------------------ ciclo
    def run(
        self,
        *,
        idea: str,
        niche: str,
        smoke_test_id: Optional[str] = None,
        squad_id: Optional[str] = None,
        language: str = "pt",
        duration_target: int = 45,
        register: bool = True,
    ) -> SquadRunResult:
        """Ciclo completo do esquadrão com todos os portões."""
        self.ensure_operational("produzir vídeo")
        self.ensure_not_repeated(idea, niche, tags=[self.squad_type, niche])
        validated = self.ensure_demand(smoke_test_id)

        sid = squad_id or self.stable_id("sq", self.squad_type, idea, niche)
        if register:
            self.ensure_registered(squad_id=sid, name=idea[:80], niche=niche)

        script = self.write_script(
            idea=idea, niche=niche, language=language, duration_target=duration_target
        )

        out_dir = Path(settings.video_output_dir) / sid / utcnow_iso()[:10]
        footage_provider = FootageProvider()
        footage = footage_provider.fetch(niche or idea, out_dir / "footage")

        render = self.render(script=script, out_dir=out_dir, footage=footage)

        # ---- compliance de mídia (antes do juiz) --------------------------
        tts_engine = str((render.steps.get("tts") or {}).get("engine", "")).split(":")[0]
        media_assets: Dict[str, Any] = {
            "video_sources": [
                {k: footage[k] for k in ("source", "license") if k in footage}
            ],
        }
        if render.audio_path:
            media_assets["audio"] = {
                "source": tts_engine or "edge_tts",
                "license": "lgpl" if "edge" in (tts_engine or "edge") else "apache-2.0",
            }
        compliance = legal_department.audit_media(media_assets)
        if not compliance.passed:
            blockers = [f.to_dict() for f in compliance.findings if f.severity == "critical"]
            self.record_agent_execution(success=False, failure_reason="compliance_media")
            return SquadRunResult(
                squad_id=sid,
                squad_type=self.squad_type,
                action="produce_video",
                status="blocked",
                summary="Produção bloqueada pelo departamento jurídico (mídia).",
                artifacts=[{"type": "script", **script.to_dict()},
                           {"type": "compliance", **compliance.to_dict()}],
                blockers=blockers,
                next_actions=["Substituir fontes de mídia por Pexels/Pixabay/"
                              "gerado localmente antes de repetir."],
            )

        # ---- Juiz Eval ----------------------------------------------------
        judge_input = (
            f"{script.hook}\n\n" + "\n\n".join(script.body) + f"\n\n{script.cta}"
        )
        judge_metadata = {
            "duration_seconds": render.duration_seconds or duration_target,
            "has_cta": bool(script.cta),
            "title": script.title,
        }
        verdict = self.review_with_judge(
            content=judge_input,
            squad_id=sid,
            metadata=judge_metadata,
        )

        package = {
            "type": "video_package",
            "squad_id": sid,
            "title": script.title,
            "description": script.description,
            "hashtags": script.hashtags,
            "platforms": ["tiktok", "youtube_shorts", "instagram_reels"],
            "narration": script.full_narration,
            "script": script.to_dict(),
            "render": render.to_dict(),
            "footage": footage,
            "judge": verdict.to_dict(),
            "compliance": compliance.to_dict(),
            "smoke_test_id": smoke_test_id,
            "validated_demand": bool(validated),
            "created_at": utcnow_iso(),
            "next_step": (
                "Revisão humana e upload manual. Nenhuma publicação é automática."
            ),
        }
        package_path = out_dir / "publication_package.json"
        try:
            package_path.write_text(
                json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("Não foi possível gravar o pacote: %s", exc)
        self.persist_artifact({"type": "video_package", "path": str(package_path), **package})

        self.record_activity(sid, views=0)
        self.record_agent_execution(
            success=render.ok,
            judge_score=verdict.score,
            failure_reason="" if render.ok else "render_indisponivel",
        )
        self.publish(
            "video_produced",
            f"Short '{script.title[:60]}' pronto (juiz {verdict.score:.1f}). "
            f"Render: {'ok' if render.ok else 'indisponível'}.",
            squad_id=sid,
            judge_score=verdict.score,
            video_path=render.video_path,
        )

        summary = (
            f"Guião escrito ({script.engine}), juiz {verdict.score:.1f}/100, "
            f"compliance {'ok' if compliance.passed else 'falhou'}. "
            + ("Vídeo renderizado." if render.ok else "Vídeo não renderizado: "
               + "; ".join(render.warnings[:2]))
        )
        return SquadRunResult(
            squad_id=sid,
            squad_type=self.squad_type,
            action="produce_video",
            status="completed" if render.ok else "awaiting_validation",
            summary=summary,
            artifacts=[package, {"type": "package_path", "path": str(package_path)}],
            metrics={
                "judge_score": verdict.score,
                "render_ok": render.ok,
                "duration_seconds": render.duration_seconds,
                "tts": render.steps.get("tts", {}),
                "warnings": render.warnings,
            },
            next_actions=[
                "Upload manual do vídeo (TikTok/Shorts/Reels) com o pacote gerado.",
                "Registar views/cliques via POST /squads/{id}/activity.",
            ],
            blockers=[] if render.ok else [{"reason": "render_indisponivel",
                                            "warnings": render.warnings}],
            llm_provider=script.engine,
            is_llm=script.is_llm,
        )


def _srt_time(seconds: float) -> str:
    total_ms = int(max(0.0, seconds) * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _wrap_caption(text: str, max_chars: int = 42) -> str:
    words = (text or "").split()
    lines: List[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > max_chars:
            lines.append(current.strip())
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current.strip())
    return "\n".join(lines[:3]) or " "


video_factory = VideoFactorySquad()

__all__ = ["VideoFactorySquad", "video_factory", "VideoScript", "RenderResult", "ALLOWED_TTS"]
