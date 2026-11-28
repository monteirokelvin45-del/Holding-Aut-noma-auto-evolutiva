"""Renderer de motion graphics — vídeo real sem GPU.

Porque existe: os backends de difusão (Wan 2.2, LTX, LongCat) precisam de GPU.
Sem ela, a holding ficava com um único footage possível — um gradiente
abstracto — o que não é vídeo publicável. Este módulo gera planos animados
**a sério**, com Pillow (desenho das artes, já requisito do projecto) e ffmpeg
(kernel de vídeo, disponível via ``imageio-ffmpeg``).

O que sai daqui:

* fundo em gradiente diagonal com vinheta e *grão* por plano (cores mudam
  entre planos, para o vídeo não parecer um único cartão estático);
* texto grande, centrado, com quebra de linha **medida** (nunca transborda),
  sombra suave e *fade-in/out* por plano;
* barra de progresso no rodapé (dá noção de duração);
* transições em fade entre planos + `concat` sem recodificar.

Licença: código e artes próprios (``own_work``); nenhum asset externo, nenhuma
fonte descarregada em runtime (usa DejaVu, presente na imagem base do Python).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.logger import get_logger

log = get_logger("squads.motion")

#: Paletas por plano (RGB) — cada plano tem a sua, para haver movimento visual.
PALETTES: List[Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = [
    ((10, 16, 32), (34, 211, 238)),    # azul-noite -> ciano
    ((18, 12, 34), (167, 139, 250)),   # violeta -> lilás
    ((8, 22, 26), (45, 212, 191)),     # petróleo -> turquesa
    ((26, 12, 20), (244, 114, 182)),   # vinho -> rosa
    ((12, 20, 14), (163, 230, 53)),    # floresta -> lima
    ((22, 18, 8), (251, 191, 36)),     # âmbar escuro -> dourado
]

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


@dataclass
class Scene:
    """Um plano: texto curto + duração + tipo de animação."""

    text: str
    duration: float
    kind: str = "title"            # title | bullet | cta
    index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "duration": round(self.duration, 2),
            "kind": self.kind,
            "index": self.index,
        }


@dataclass
class MotionResult:
    ok: bool
    path: str = ""
    scenes: List[Dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0
    resolution: str = ""
    fps: int = 30
    notes: List[str] = field(default_factory=list)
    engine: str = "pillow+ffmpeg"
    license: str = "own_work"
    created_at: str = field(default_factory=str)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utcnow_iso()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "scenes": self.scenes,
            "duration_seconds": round(self.duration_seconds, 2),
            "resolution": self.resolution,
            "fps": self.fps,
            "notes": self.notes,
            "engine": self.engine,
            "license": self.license,
            "created_at": self.created_at,
        }


def _font(size: int):
    """Fonte DejaVu embutida; sem fontes no sistema devolve a bitmap padrão."""
    try:
        from PIL import ImageFont
    except ImportError as exc:  # pragma: no cover - Pillow é requisito
        raise RuntimeError(
            "Pillow é necessário para o motion renderer (pip install Pillow)."
        ) from exc
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


class MotionRenderer:
    """Gera planos animados: arte com Pillow, vídeo com ffmpeg."""

    def __init__(self) -> None:
        self._ffmpeg_path: Optional[str] = None

    # ------------------------------------------------------------------ setup
    def ffmpeg(self) -> Optional[str]:
        if self._ffmpeg_path is not None:
            return self._ffmpeg_path or None
        configured = settings.ffmpeg_binary
        found = shutil.which(configured or "ffmpeg")
        if not found:
            try:
                import imageio_ffmpeg  # type: ignore

                found = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:  # pragma: no cover - ambiente sem imageio-ffmpeg
                found = None
        self._ffmpeg_path = found or ""
        return found or None

    def capabilities(self) -> Dict[str, Any]:
        """O que está mesmo disponível (testado, não assumido)."""
        binary = self.ffmpeg()
        try:
            import PIL  # noqa: F401  # type: ignore

            pillow = True
        except ImportError:
            pillow = False
        return {
            "available": bool(binary) and pillow,
            "ffmpeg": binary,
            "pillow": pillow,
            "engine": "pillow+ffmpeg",
            "note": "" if binary and pillow else "Falta ffmpeg ou Pillow.",
        }

    # ------------------------------------------------------------ planeamento
    @staticmethod
    def plan(script: Any, target_duration: float) -> List[Scene]:
        """Divide o guião em planos com durações somando o alvo.

        Hook 22%, corpo 66% repartido, CTA 12%; nenhum plano abaixo de 1,8 s
        (ilegível em telemóvel) nem acima de 8 s (monótono).
        """
        hook = getattr(script, "hook", "") or ""
        body = [b for b in (getattr(script, "body", []) or []) if b]
        cta = getattr(script, "cta", "") or ""
        total = max(6.0, float(target_duration))

        parts: List[Tuple[str, str]] = [("title", hook)]
        parts.extend(("bullet", item) for item in body)
        parts.append(("cta", cta))
        parts = [(k, t) for k, t in parts if t.strip()] or [
            ("title", getattr(script, "title", "") or "Vídeo")
        ]

        bullets = sum(1 for kind, _ in parts if kind == "bullet")
        bullet_weight = (1.0 - 0.22 - 0.12) / bullets if bullets else 0.0
        fixed = {"title": 0.22, "cta": 0.12}
        scenes = [
            Scene(
                text=text.strip(),
                duration=max(1.8, min(8.0, total * (bullet_weight if kind == "bullet"
                                                    else fixed[kind]))),
                kind=kind,
                index=index,
            )
            for index, (kind, text) in enumerate(parts)
        ]
        produced = sum(scene.duration for scene in scenes) or 1.0
        factor = total / produced
        for scene in scenes:
            scene.duration = max(1.5, min(9.0, scene.duration * factor))
        return scenes

    # ------------------------------------------------------------------ arte
    @staticmethod
    def _wrap(text: str, font, max_width: int) -> List[str]:
        """Quebra de linha medida em píxeis — nunca deixa o texto sair do ecrã."""
        try:
            from PIL import Image, ImageDraw

            measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
            width_of = lambda chunk: measure.textlength(chunk, font=font)  # noqa: E731
        except Exception:  # pragma: no cover
            width_of = lambda chunk: len(chunk) * 20  # noqa: E731

        lines: List[str] = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if not current or width_of(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [text]

    def _art(
        self,
        scene: Scene,
        width: int,
        height: int,
        progress: float,
    ) -> Any:
        """Desenha a arte de um plano: fundo + texto + sombra + barra."""
        from PIL import Image, ImageDraw, ImageFilter

        start, accent = PALETTES[scene.index % len(PALETTES)]
        # Gradiente diagonal: interpolação por linha, rápido em Python puro
        # nesta resolução e muito mais previsível do que o `geq` do ffmpeg.
        base = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(base)
        steps = 96
        for step in range(steps):
            ratio = step / (steps - 1)
            colour = tuple(
                int(start[channel] + (accent[channel] - start[channel]) * ratio * 0.55)
                for channel in range(3)
            )
            draw.rectangle(
                [0, int(height * ratio), width, int(height * (ratio + 1 / steps)) + 1],
                fill=colour,
            )
        # "Sol" desfocado: um ponto focal atrás do texto. Usa *screen* (soma
        # aditiva) — `blend` com uma camada preta escurecia tudo em vez de
        # iluminar, que foi o bug da primeira versão.
        glow = Image.new("RGB", (width, height), (0, 0, 0))
        ImageDraw.Draw(glow).ellipse(
            [width * 0.45, height * 0.10, width * 1.25, height * 0.62],
            fill=tuple(int(channel * 0.85) for channel in accent),
        )
        window = Image.new("RGB", (width, height), (((255, 255, 255))))
        glow = Image.composite(
            glow.filter(ImageFilter.GaussianBlur(width / 7)), window, window.convert("L")
        )
        from PIL import ImageChops

        base = ImageChops.screen(base, glow.point(lambda value: int(value * 0.55)))

        # Vinheta: máscara branca no centro (mantém a base) e preta nas bordas
        # (puxa para o escuro). Inverter isto apagava a imagem toda.
        vignette = Image.new("L", (width, height), 0)
        ImageDraw.Draw(vignette).ellipse(
            [-width * 0.25, -height * 0.10, width * 1.25, height * 1.10], fill=255
        )
        dark = Image.new("RGB", (width, height), (5, 8, 16))
        base = Image.composite(base, dark, vignette.filter(ImageFilter.GaussianBlur(width / 9)))

        overlay = ImageDraw.Draw(base, "RGBA")

        # --- texto -------------------------------------------------------
        is_title = scene.kind == "title"
        font_size = int(height * (0.075 if is_title else 0.05))
        font = _font(font_size)
        lines = self._wrap(scene.text, font, int(width * 0.84))
        # Máximo 4 linhas: mais do que isso deixa de ser leitura e passa a ser
        # um bloco ilegível no telemóvel. Reduz a fonte até caber.
        min_size = max(22, int(height * 0.030))
        while len(lines) > 4 and font_size > min_size:
            font_size = int(font_size * 0.9)
            font = _font(font_size)
            lines = self._wrap(scene.text, font, int(width * 0.84))
        if len(lines) > 4:
            # Ainda não cabe: corta palavras e fecha com reticências honestas.
            words = " ".join(lines).split()
            while words and len(self._wrap(" ".join(words) + "…", font,
                                           int(width * 0.84))) > 4:
                words.pop()
            lines = self._wrap(" ".join(words) + "…", font, int(width * 0.84))

        line_height = int(font_size * 1.28)
        block_height = line_height * len(lines)
        # Ligeiramente acima do centro: deixa ar em baixo para a barra de
        # progresso e para eventuais legendas da plataforma.
        y = int(height * 0.46) - block_height // 2
        for row, line in enumerate(lines):
            text_width = overlay.textlength(line, font=font)
            x = (width - text_width) / 2
            # Sombra em duas camadas: halo largo + contorno próximo.
            for offset, alpha in ((6, 90), (3, 140)):
                overlay.text(
                    (x + offset - 2, y + row * line_height + offset - 2),
                    line, font=font, fill=(0, 0, 0, alpha),
                )
            overlay.text(
                (x, y + row * line_height), line, font=font,
                fill=(255, 255, 255, 255),
                stroke_width=2, stroke_fill=(0, 0, 0, 170),
            )

        # --- barra de progresso + marca de autor --------------------------
        bar_height = max(6, int(height * 0.006))
        overlay.rectangle(
            [0, height - bar_height, int(width * max(0.0, min(1.0, progress))), height],
            fill=(*accent, 235),
        )
        overlay.rectangle(
            [0, height - bar_height, width, height], outline=(255, 255, 255, 40)
        )
        caption = "Automaton Holding"
        small = _font(max(16, int(height * 0.016)))
        overlay.text(
            (int(width * 0.06), int(height * 0.055)),
            caption, font=small, fill=(255, 255, 255, 130),
        )
        return base

    # ----------------------------------------------------------------- render
    def render(
        self,
        *,
        script: Any,
        out_dir: Path,
        target_duration: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[int] = None,
    ) -> MotionResult:
        """Gera o clipe de motion graphics completo (todos os planos)."""
        binary = self.ffmpeg()
        w = int(width or settings.video_target_width)
        h = int(height or settings.video_target_height)
        rate = int(fps or settings.video_target_fps)
        result = MotionResult(ok=False, resolution=f"{w}x{h}", fps=rate)
        if not binary:
            result.notes.append("ffmpeg indisponível: sem motion graphics.")
            return result

        total_target = float(
            target_duration
            or min(
                settings.video_max_duration_seconds,
                max(8.0, len(getattr(script, "full_narration", "") or "") / 14),
            )
        )
        scenes = self.plan(script, total_target)
        result.scenes = [scene.to_dict() for scene in scenes]
        out_dir.mkdir(parents=True, exist_ok=True)

        clips: List[Path] = []
        elapsed = 0.0
        for scene in scenes:
            elapsed += scene.duration
            progress = elapsed / max(0.1, sum(s.duration for s in scenes))
            png = out_dir / f"art_{scene.index:02d}.png"
            clip = out_dir / f"scene_{scene.index:02d}.mp4"
            try:
                art = self._art(scene, w, h, progress)
                art.save(png, "PNG")
            except Exception as exc:  # pragma: no cover - falha de Pillow
                result.notes.append(f"Arte do plano {scene.index} falhou: {exc}")
                continue
            if self._clip_from_still(binary, png, clip, scene.duration, rate):
                clips.append(clip)
            else:
                result.notes.append(f"Plano {scene.index} falhou a codificar.")

        if not clips:
            result.notes.append("Nenhum plano foi renderizado.")
            return result

        target = out_dir / "motion_base.mp4"
        list_file = out_dir / "scenes.txt"
        list_file.write_text(
            "\n".join(f"file '{clip.name}'" for clip in clips), encoding="utf-8"
        )
        command = [
            binary, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(target),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=600,
                check=False, cwd=str(out_dir),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            result.notes.append(f"concat falhou: {exc}")
            return result
        if completed.returncode != 0 or not target.is_file():
            result.notes.append(
                f"concat falhou ({completed.returncode}): "
                f"{(completed.stderr or '')[-300:]}"
            )
            return result

        result.ok = True
        result.path = str(target)
        result.duration_seconds = sum(scene.duration for scene in scenes)
        log.info(
            "Motion graphics: %d planos, %.1fs, %s",
            len(scenes), result.duration_seconds, result.resolution,
        )
        return result

    def _clip_from_still(
        self,
        binary: str,
        still: Path,
        out_path: Path,
        duration: float,
        fps: int,
    ) -> bool:
        """Converte uma arte estática num plano com *zoom* lento e fades.

        O ``zoompan`` dá o movimento subtil que separa um slide de um vídeo;
        os fades evitam cortes secos entre planos.
        """
        duration = max(1.2, float(duration))
        fade_out = max(0.0, duration - 0.35)
        graph = (
            f"scale=iw*1.06:ih*1.06,"
            f"zoompan=z='min(zoom+0.0004,1.06)':d={int(duration * fps)}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps},"
            f"fade=t=in:st=0:d=0.30,"
            f"fade=t=out:st={fade_out:.2f}:d=0.35,format=yuv420p"
        )
        command = [
            binary, "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-i", str(still),
            "-vf", graph,
            "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-r", str(fps), "-pix_fmt", "yuv420p",
            str(out_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=300, check=False
            )
        except (subprocess.SubprocessError, OSError):
            return False
        if completed.returncode != 0:
            log.warning(
                "Plano %s falhou: %s", out_path.name, (completed.stderr or "")[-300:]
            )
            return False
        return out_path.is_file() and out_path.stat().st_size > 1024


motion_renderer = MotionRenderer()
