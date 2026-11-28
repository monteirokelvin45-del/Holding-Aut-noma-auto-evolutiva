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
from .motion_renderer import motion_renderer
from ..core.llm_router import LLMRequest, llm_router
from ..core.logger import get_logger
from ..governance.legal_compliance import legal_department
from .ai_video_backends import build_visual_prompt, genai_backends
from .squad_base import BaseSquad, SquadRunResult

log = get_logger("squads.video")

ALLOWED_TTS = {"edge_tts", "kokoro", "chatterbox", "piper", "espeak_ng"}
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
            # Kokoro-82M offline com vozes PT: modelo ONNX + 54 vozes locais.
            "kokoro": (
                self._module("kokoro") or self._module("kokoro_onnx")
                or self._kokoro_local_pronto()
            ),
            "chatterbox": self._module("chatterbox"),
            "piper": shutil.which("piper") is not None,
            # espeak-ng (GPLv3) via libespeakng-loader: síntese offline, inclui
            # português. Usado como último recurso quando não há rede nem
            # pesos neurais — a voz é robótica, e isso fica declarado.
            "espeak_ng": self._module("espeakng_loader"),
        }

    @staticmethod
    def _kokoro_local_pronto() -> bool:
        """Kokoro-82M local (ONNX + vozes PT) presente e utilizável."""
        try:
            from .kokoro_tts import kokoro_local

            return kokoro_local.disponivel()[0]
        except Exception:
            return False

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
        # O motor local com vozes PT vem primeiro: é o único que produz
        # português de qualidade sem rede. `kokoro_onnx` (en/zh) e `kokoro`
        # (PyTorch, precisa do HuggingFace) ficam como alternativas.
        if engine in {"kokoro", "edge_tts"} and self._kokoro_local_pronto():
            ok, message = self._kokoro_local(text, out_path, language)
            if ok:
                return True, message
            log.warning("Kokoro local falhou (%s); a tentar alternativas.", message)
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
        if self._module("espeakng_loader"):
            ok, message = self._espeak_ng(text, out_path, language)
            if ok:
                return True, "espeak_ng"

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
    def _espeak_ng(text: str, out_path: Path, language: str) -> Tuple[bool, str]:
        """Síntese offline com espeak-ng (GPLv3, via ``espeakng-loader``).

        É o único motor que funciona **sem rede e sem pesos neurais**, e o único
        com voz portuguesa instalada neste ambiente. A qualidade é robótica —
        por isso vem por último e o motor devolvido diz ``espeak_ng`` para o
        registo ficar honesto sobre o que gerou o áudio.
        """
        try:
            import array
            import ctypes as C
            import wave

            import espeakng_loader  # type: ignore

            lib = C.CDLL(espeakng_loader.get_library_path())
            rate = lib.espeak_Initialize(
                0x02, 0, espeakng_loader.get_data_path().encode(), 0
            )
            if rate <= 0:
                return False, "espeak_ng:init_falhou"
            voice = b"pt" if language.startswith("pt") else b"en"
            if lib.espeak_SetVoiceByName(voice) != 0:
                return False, f"espeak_ng:voz_{voice.decode()}_ausente"

            buffer = array.array("h")
            callback_type = C.CFUNCTYPE(
                C.c_int, C.POINTER(C.c_short), C.c_int, C.c_uint, C.c_void_p
            )

            @callback_type
            def on_wave(wav, count, events, user):  # noqa: ANN001
                if wav and count > 0:
                    buffer.extend(wav[i] for i in range(count))
                return 0

            lib.espeak_SetSynthCallback(on_wave)
            payload = text.encode("utf-8")
            rc = lib.espeak_Synth(
                payload, len(payload) + 1, 0, 0, 0, 0, None, None
            )
            if rc != 0 or not buffer:
                return False, f"espeak_ng:rc_{rc}"
            with wave.open(str(out_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(buffer.tobytes())
            return True, "espeak_ng:ok"
        except Exception as exc:
            return False, f"espeak_ng:{exc}"

    #: Modelo e vozes descarregados em ``models/kokoro/`` (ver docs/stack_audit.md).
    KOKORO_MODEL = Path("models/kokoro/kokoro-v1.0.q8.onnx")
    KOKORO_VOICES = Path("models/kokoro/voices-v1.0.npz")

    @staticmethod
    def _configure_phonemizer() -> Optional[str]:
        """Aponta o ``phonemizer`` para a ``libespeak-ng.so`` do espeakng-loader.

        O Kokoro converta texto em fonemas via ``phonemizer``, que por omissão
        procura a espeak-ng *no sistema*. Não há binário nem ``.so`` em
        ``/usr/lib`` neste ambiente: só existe a cópia que vem dentro do
        pacote ``espeakng_loader``. Sem esta configuração o ``phonemizer``
        instancia a biblioteca do sistema e falha com
        ``language "en" is not supported by the espeak backend`` — mensagem
        enganadora, porque o problema é a biblioteca não estar carregada.

        Devolve ``None`` em caso de sucesso, ou a razão da falha.
        """
        try:
            import espeakng_loader  # type: ignore
            from phonemizer.backend.espeak.wrapper import (  # type: ignore
                EspeakWrapper,
            )
        except ImportError as exc:
            return f"phonemizer_indisponivel:{exc}"
        if getattr(TTSRegistry, "_phonemizer_ok", False):
            return None
        try:
            EspeakWrapper.set_library(espeakng_loader.get_library_path())
            EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
        except Exception as exc:  # noqa: BLE001 - reportar, não rebentar
            return f"phonemizer_config:{exc}"
        TTSRegistry._phonemizer_ok = True
        return None

    @staticmethod
    def _kokoro_local(text: str, out_path: Path, language: str) -> Tuple[bool, str]:
        """Kokoro-82M offline com vozes portuguesas (pf_dora / pm_alex)."""
        from .kokoro_tts import VOZES_PT, kokoro_local

        voz = VOZES_PT["feminina"] if language.startswith("pt") else "af_heart"
        ok, motivo = kokoro_local.sintetizar(text, out_path, voz=voz)
        if not ok:
            return False, motivo
        return out_path.is_file() and out_path.stat().st_size > 1024, motivo

    @staticmethod
    def _kokoro(text: str, out_path: Path, language: str) -> Tuple[bool, str]:
        """Síntese offline com Kokoro-82M (Apache-2.0, ONNX int8).

        Duas variantes: ``kokoro`` (PyTorch, precisa de pesos do HuggingFace) e
        ``kokoro_onnx`` (ONNX, corre só com os ficheiros locais). A segunda é a
        que funciona sem rede — e é a que existe neste repositório.
        """
        try:
            import numpy as np  # type: ignore

            try:
                from kokoro import KPipeline  # type: ignore

                pipeline = KPipeline(lang_code="p" if language.startswith("pt") else "a")
                chunks = [
                    audio
                    for _, _, audio in pipeline(
                        text, voice="pf_dora" if language.startswith("pt") else "af_heart"
                    )
                ]
                if not chunks:
                    return False, "kokoro:sem_audio"
                import soundfile as sf  # type: ignore

                sf.write(str(out_path), np.concatenate(chunks), 24000)
                return True, "kokoro:ok"
            except ImportError:
                from kokoro_onnx import Kokoro  # type: ignore

                model = TTSRegistry.KOKORO_MODEL
                voices = TTSRegistry.KOKORO_VOICES
                if not model.is_file() or not voices.is_file():
                    return False, (
                        "kokoro_onnx:pesos_ausentes (esperado "
                        f"{model} + {voices})"
                    )
                # O phonemizer tem de saber onde está a libespeak-ng.so antes
                # do primeiro ``create``; caso contrário o Kokoro nem arranca.
                phonemizer_error = TTSRegistry._configure_phonemizer()
                if phonemizer_error:
                    return False, f"kokoro_onnx:{phonemizer_error}"
                kokoro = Kokoro(str(model), str(voices))
                # Esta exportação espera style com rank 2 ([1, 256]); a lib
                # devolve [256] e o onnxruntime recusa. Compat explícita.
                kokoro._style_for = (  # noqa: SLF001 - correcção de forma
                    lambda voice, length: np.asarray(
                        voice[min(length, len(voice)) - 1], dtype=np.float32
                    ).reshape(1, 256)
                )
                # Não há vozes portuguesas no pacote de vozes v1.0 (só en/zh).
                # Usar uma voz inglesa a ler português sairia incompreensível:
                # devolvemos falha explícita e o registo passa ao próximo motor.
                if language.startswith("pt"):
                    return False, (
                        "kokoro_onnx:sem_voz_pt (o pacote voices-v1.0 só tem "
                        "vozes en/zh; usar edge-tts para português)"
                    )
                samples, rate = kokoro.create(
                    text, voice="af_heart", speed=1.0, lang="en-us"
                )
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

    def fetch(
        self,
        query: str,
        out_dir: Path,
        index: int = 0,
        *,
        script: Optional["VideoScript"] = None,
    ) -> Dict[str, Any]:
        """Escolhe o footage por esta ordem, sempre com proveniência.

        1. **IA local** (Wan 2.2 / LongCat / Kandinsky / LTX) quando há backend
           configurado — é footage original, sem dependência de banco de imagens;
        1b. **motion graphics próprios** (Pillow + ffmpeg): planos animados com
           o texto do guião. Funciona **sem GPU** e é o que garante que existe
           vídeo real mesmo sem pesos de difusão;
        2. **Pexels** (licença gratuita para uso comercial);
        3. **Pixabay** (idem);
        4. **fundo procedimental** gerado por ffmpeg (declarado como tal).
        """
        out_dir.mkdir(parents=True, exist_ok=True)

        # ---- 1. geração por IA aberta (custo $0.00, corre em casa) ---------
        if settings.genai_video_enabled:
            generation = genai_backends.generate(
                build_visual_prompt(query, query),
                out_dir=out_dir / "genai",
                kind="video",
                duration_seconds=float(settings.video_max_duration_seconds) / 6.0,
                seed=index + 7,
            )
            if generation.ok:
                item = generation.as_footage(query)
                self.sources.append(item)
                return item
            log.warning(
                "Geração por IA indisponível (%s); a usar banco de imagens.",
                generation.reason,
            )

        # ---- 1b. motion graphics próprios (sem GPU, sempre disponível) -----
        item = self._motion(query, out_dir, index, script=script)
        if item:
            self.sources.append(item)
            return item

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

    def _motion(
        self,
        query: str,
        out_dir: Path,
        index: int,
        *,
        script: Optional["VideoScript"] = None,
    ) -> Optional[Dict[str, Any]]:
        """Clipe de motion graphics gerado por nós (licença própria, sem GPU).

        Só entra quando o renderer diz que consegue: ffmpeg + Pillow. Se falhar,
        devolve ``None`` e a cadeia continua — nunca bloqueia a produção.
        """
        caps = motion_renderer.capabilities()
        if not caps.get("available"):
            return None
        # Sem guião não há texto para animar: o motion graphics não se aplica
        # (ex.: chamadas de teste). Nesse caso caímos para a fonte seguinte.
        if script is None:
            return None
        try:
            motion = motion_renderer.render(
                script=script,
                out_dir=out_dir / "motion",
                target_duration=min(
                    float(settings.video_max_duration_seconds),
                    max(6.0, len(getattr(script, "full_narration", "") or "") / 14),
                ),
            )
        except Exception as exc:  # pragma: no cover - falha inesperada
            log.warning("Motion graphics falhou: %s", exc)
            return None
        if not motion.ok:
            log.warning("Motion graphics indisponível: %s", "; ".join(motion.notes[:2]))
            return None
        return {
            "source": "generated",
            "kind": "motion_graphics",
            "path": motion.path,
            "license": "own_work",
            "url": "",
            "query": query,
            "engine": motion.engine,
            "scenes": len(motion.scenes),
            # O motion graphics já tem o texto queimado: queimar legendas SRT
            # por cima duplicava o texto e tornava-o ilegível.
            "has_text": True,
            "note": (
                f"{len(motion.scenes)} planos animados gerados localmente "
                f"(Pillow + ffmpeg), sem GPU e sem material de terceiros."
            ),
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
        # O modelo pequeno (0.6B) ignora instruções genéricas de idioma e
        # chega a copiar o slug do nicho para dentro do texto — foi assim que
        # um vídeo saiu a dizer «perde dinheiro em freelancing_video_kokoro».
        # Duas defesas: (a) o idioma vai em maiúsculas e com exemplo de como
        # NÃO escrever; (b) o `niche` deixa de ser um campo do prompt e passa
        # a contexto entre parênteses, para não ser lido como material.
        prompt = (
            "Escreve um guião de vídeo vertical (9:16) para TikTok/Shorts/Reels.\n"
            f"IDEIA: {idea}\n"
            f"(contexto: {niche.replace('_', ' ')}, não escrever este texto)\n"
            f"ALVO: ~{words_target} palavras de narração (~{duration_target}s).\n"
            "ESCREVE TODO O CONTEÚDO EM PORTUGUÊS DE MOÇAMBIQUE. "
            "title, hook, body, cta e description têm de estar em português. "
            "Não uses palavras em inglês. Não escrevas nomes técnicos nem "
            "identificadores com underscores.\n"
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
            # O modelo devolve, às vezes, objectos em vez de frases:
            #   [{"title": "...", "description": "...", "content": "..."}]
            # `str(b)` disso põe JSON cru na narração — e a voz lê chavetas.
            # Medido num guião real, que saiu a dizer «{'title': 'Conheça o
            # nicho...'}» em voz alta. Aqui só entram frases.
            body = [self._so_texto(b) for b in body]
            body = [b for b in body if b]
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
        # A narração NÃO interpola `niche`: um slug como
        # «freelancing_video_kokoro» dentro de uma frase falada é lixo audível,
        # e foi exactamente o que a Kokoro chegou a ler em voz alta.
        nicho_legivel = niche.replace("_", " ").strip().lower()
        return VideoScript(
            title=f"{idea[:80]}",
            hook=(
                "Sabias que a maioria das pessoas que começa a trabalhar por "
                "conta própria perde dinheiro nos primeiros meses, sem "
                "perceber porquê?"
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

    def produzir_offline(
        self,
        *,
        script: "VideoScript",
        out_dir: Path,
        language: str = "pt",
    ) -> Optional[Dict[str, Any]]:
        """Vídeo completo desenhado frame a frame, sem rede.

        Diferença para `render()`: aqui o fundo não é uma imagem estática com
        zoom no FFmpeg — cada frame é desenhado (gradiente a rodar, formas a
        orbitar, barra de progresso) e a voz é sintetizada frase a frase, pelo
        que as legendas usam a duração REAL de cada fala em vez de uma
        estimativa por contagem de caracteres.

        Devolve o dicionário do resultado, ou ``None`` se o pipeline não
        arrancar (aí o chamador usa `render()` como antes).
        """
        try:
            from .offline_video_pipeline import offline_pipeline
        except Exception as exc:  # Pillow/ffmpeg em falta
            log.info("Pipeline offline indisponível: %s", exc)
            return None

        frases = [script.hook] + [b for b in script.body if b] + [script.cta]
        frases = [f.strip() for f in frases if f and f.strip()]
        resultado = offline_pipeline.produzir(
            frases=frases, titulo=script.title, out_dir=out_dir, language=language
        )
        return resultado.to_dict()

    @staticmethod
    def _so_texto(valor: Any) -> str:
        """Extrai uma frase legível de um item que pode ser dict ou lista.

        Um guião com JSON na narração é pior do que um guião vazio: é áudio
        publicado. Se o item for um dicionário, usa o campo de texto mais
        longo; se for uma lista, junta as partes.
        """
        if isinstance(valor, str):
            return valor.strip()[:280]
        if isinstance(valor, dict):
            textos = [
                str(v).strip()
                for v in valor.values()
                if isinstance(v, str) and str(v).strip()
            ]
            if not textos:
                return ""
            # `content`/`description` são mais informativos do que `title`.
            preferidos = [
                str(valor.get(chave, "")).strip()
                for chave in ("content", "text", "body", "description", "title")
                if str(valor.get(chave, "")).strip()
            ]
            escolhido = preferidos[0] if preferidos else max(textos, key=len)
            return escolhido[:280]
        if isinstance(valor, (list, tuple)):
            partes = [VideoFactorySquad._so_texto(v) for v in valor]
            return " ".join(p for p in partes if p).strip()[:280]
        return ""

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
        # Sem legendas sobre footage que já tem texto (motion graphics): duas
        # camadas de texto sobrepostas tornam o vídeo ilegível.
        if srt_path.is_file() and not footage.get("has_text"):
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
        footage = footage_provider.fetch(
            niche or idea, out_dir / "footage", script=script
        )

        # Pipeline offline primeiro: vídeo desenhado frame a frame, voz Kokoro
        # frase a frase e legendas com tempos reais. Se faltar Pillow ou
        # ffmpeg, cai no `render()` antigo (imagem estática + zoom).
        offline = self.produzir_offline(script=script, out_dir=out_dir, language=language)
        render = self.render(script=script, out_dir=out_dir, footage=footage)

        if offline and offline.get("ok"):
            # O resultado offline substitui o do renderer: tem áudio próprio,
            # legendas com tempos medidos e movimento real.
            render.ok = True
            render.path = offline["video"]
            render.audio_path = offline.get("audio") or render.audio_path
            render.duration_seconds = offline.get("duracao_s") or render.duration_seconds
            render.steps = {
                "tts": {"ok": True, "engine": offline.get("tts_engine"),
                        "ms": offline.get("ms_tts")},
                "subtitles": {"ok": bool(offline.get("srt")), "path": offline.get("srt")},
                "render": {"ok": True, "ffmpeg": "imageio-ffmpeg",
                           "ms": offline.get("ms_render"),
                           "frames": offline.get("frames"),
                           "pipeline": "offline_frame_by_frame"},
            }
            render.warnings = list(offline.get("notas") or [])
        elif offline:
            render.warnings = list(render.warnings) + [
                f"pipeline offline: {n}" for n in (offline.get("notas") or [])
            ]

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

        # Gate de áudio: um vídeo sem narração não é publicável. O pacote
        # continua a ser gerado (é útil para revisão), mas fica marcado.
        audio_ok = bool(render.audio_path)
        publishable = bool(render.ok and audio_ok and compliance.passed)
        package = {
            "type": "video_package",
            "publishable": publishable,
            "publish_blockers": (
                [] if publishable else [
                    item for item in (
                        None if render.ok else "render_indisponivel",
                        None if audio_ok else "sem_narracao (TTS indisponível)",
                        None if compliance.passed else "compliance_falhou",
                    ) if item
                ]
            ),
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
                if publishable else
                "BLOQUEADO: resolver " + ", ".join(
                    item for item in (
                        None if render.ok else "render",
                        None if audio_ok else "narração",
                        None if compliance.passed else "compliance",
                    ) if item
                ) + " antes de publicar."
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
            f"compliance {'ok' if compliance.passed else 'falhou'}, "
            f"áudio {'ok' if audio_ok else 'em falta'}. "
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
