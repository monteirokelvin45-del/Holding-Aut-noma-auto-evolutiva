"""Pipeline de vídeo 100% offline — a parte que o `motion_renderer` não tinha.

O que se aproveitou de `prakashdk/video-creator` e o que se deitou fora
---------------------------------------------------------------------
O repositório inspirador monta: Ollama (texto) → Stable Diffusion (imagens) →
Coqui TTS (voz) → whisperx (alinhamento) → moviepy (montagem). Das cinco
peças, **três precisam de pesos que este ambiente não alcança**
(`huggingface.co` devolve 000) e uma quarta (`moviepy`) traria `decord`/`av`.

Do que aqui se faz, nada disso é necessário:

| Peça do original | Substituto local | Porquê |
|---|---|---|
| Ollama (guião)      | Qwen3-0.6B via llama.cpp     | já instalado, sem servidor |
| Stable Diffusion    | arte procedural com Pillow   | gradientes + formas não precisam de pesos |
| Coqui TTS           | Kokoro-82M ONNX              | 54 vozes, 3 portuguesas |
| whisperx (timestamps)| duração medida por fala     | ver abaixo |
| moviepy             | FFmpeg por linha de comando  | já é dependência do projecto |

Sobre os timestamps
-------------------
O original transcreve o áudio com o whisperx para saber quando começa cada
palavra. Nós não precisamos disso: o texto é nosso, e sintetizamos **cada
frase separadamente**, medindo a duração real do WAV devolvido. O resultado é
melhor do que transcrever — é a duração exacta, não uma estimativa de um
modelo acústico — e custa zero.

Sobre o movimento
-----------------
O `motion_renderer` gera **uma imagem estática por cena** e faz-lhe um zoom
lerdo no FFmpeg. Isso é motion graphics de cartão-postal. Aqui cada frame é
desenhado: o gradiente desloca-se e as formas orbitam, com fase por cena.
30 fps × 20 s = 600 imagens 1080×1920 — pesa tempo de CPU, mas num vídeo de
20 s são ~40 s de render a 2 núcleos, e o resultado tem movimento real.
"""

from __future__ import annotations

import math
import subprocess
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config.settings import settings
from ..core.logger import get_logger
from .motion_renderer import MotionRenderer, _font

log = get_logger("squads.offline_video")

#: Paletas por cena (início, acento). Escuras de propósito: o texto branco
#: tem de ter contraste em tudo o que é telemóvel ao sol.
PALETAS: Tuple[Tuple[Tuple[int, int, int], Tuple[int, int, int]], ...] = (
    ((10, 18, 38), (56, 189, 248)),
    ((24, 12, 40), (167, 139, 250)),
    ((8, 26, 24), (52, 211, 153)),
    ((32, 14, 12), (251, 146, 60)),
    ((12, 14, 34), (244, 114, 182)),
)


@dataclass
class Fala:
    """Uma frase narrada, com a duração real do áudio gerado."""

    texto: str
    wav: Path
    inicio_s: float
    duracao_s: float

    @property
    def fim_s(self) -> float:
        return self.inicio_s + self.duracao_s


@dataclass
class ResultadoOffline:
    ok: bool
    video: Optional[Path] = None
    audio: Optional[Path] = None
    srt: Optional[Path] = None
    tts_engine: str = ""
    duracao_s: float = 0.0
    frames: int = 0
    ms_tts: int = 0
    ms_render: int = 0
    fps: int = 30
    resolucao: str = ""
    notas: List[str] = field(default_factory=list)
    falas: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "video": str(self.video) if self.video else None,
            "audio": str(self.audio) if self.audio else None,
            "srt": str(self.srt) if self.srt else None,
            "tts_engine": self.tts_engine,
            "duracao_s": round(self.duracao_s, 2),
            "frames": self.frames,
            "fps": self.fps,
            "resolucao": self.resolucao,
            "ms_tts": self.ms_tts,
            "ms_render": self.ms_render,
            "notas": self.notas,
            "falas": self.falas,
        }


def duracao_wav(caminho: Path) -> float:
    with wave.open(str(caminho), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate() or 1)


def _srt_tempo(segundos: float) -> str:
    total = max(0, int(round(segundos * 1000)))
    h, resto = divmod(total, 3_600_000)
    m, resto = divmod(resto, 60_000)
    s, ms = divmod(resto, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


class OfflineVideoPipeline:
    """Guião → voz → legendas → MP4, sem rede e sem chaves."""

    def __init__(self) -> None:
        self.renderer = MotionRenderer()

    # ------------------------------------------------------------------ voz
    def narrar(
        self, frases: Sequence[str], out_dir: Path, language: str = "pt"
    ) -> Tuple[List[Fala], str, int]:
        """Sintetiza cada frase e mede a duração real de cada uma."""
        from .video_factory_squad import TTSRegistry  # import tardio: ciclo

        registo = TTSRegistry()
        out_dir.mkdir(parents=True, exist_ok=True)
        falas: List[Fala] = []
        motor = ""
        cursor = 0.0
        inicio = time.time()

        for indice, frase in enumerate(frases):
            texto = (frase or "").strip()
            if not texto:
                continue
            # Sempre .wav: o TTSRegistry escreve WAV, e o mp3 vinha de um
            # passo extra de codificação que só acrescentava falhas.
            destino = out_dir / f"fala_{indice:02d}.wav"
            ok, detalhe = registo.synthesize(texto, destino, language=language)
            if not ok:
                log.warning("Fala %d falhou (%s); a ignorar.", indice, detalhe)
                continue
            motor = motor or detalhe
            dur = duracao_wav(destino)
            falas.append(Fala(texto=texto, wav=destino, inicio_s=cursor, duracao_s=dur))
            cursor += dur

        return falas, motor, int((time.time() - inicio) * 1000)

    # -------------------------------------------------------------- legendas
    @staticmethod
    def escrever_srt(falas: Sequence[Fala], destino: Path) -> Path:
        """SRT com os tempos reais de cada frase (não estimados)."""
        linhas: List[str] = []
        for numero, fala in enumerate(falas, start=1):
            linhas += [
                str(numero),
                f"{_srt_tempo(fala.inicio_s)} --> {_srt_tempo(fala.fim_s)}",
                fala.texto,
                "",
            ]
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text("\n".join(linhas), encoding="utf-8")
        return destino

    # ------------------------------------------------------------- desenho
    @staticmethod
    def _frame(
        indice: int,
        total: int,
        fala: Optional[Fala],
        titulo: str,
        width: int,
        height: int,
        fase: int,
    ) -> Any:
        """Um frame: gradiente em movimento, formas a orbitar, texto da fala."""
        from PIL import Image, ImageChops, ImageDraw, ImageFilter

        inicio, acento = PALETAS[fase % len(PALETAS)]
        progresso = indice / max(1, total - 1)  # 0.0 → 1.0 no vídeo todo
        t = progresso * math.tau

        # Gradiente cujo ângulo roda com o tempo: o fundo "respira".
        base = Image.new("RGB", (width, height))
        desenho = ImageDraw.Draw(base)
        passos = 48
        deslocamento = int(math.sin(t) * height * 0.12)
        for passo in range(passos):
            razao = passo / (passos - 1)
            cor = tuple(
                int(inicio[c] + (acento[c] - inicio[c]) * razao * 0.42)
                for c in range(3)
            )
            y0 = passo * height // passos + deslocamento
            desenho.rectangle(
                [0, y0, width, y0 + height // passos + 2], fill=cor
            )

        # Formas a orbitar: três elipses com fase, raio e velocidade próprios.
        #
        # Desfocar a 1080x1920 custava ~45 ms por blur, e o perfil mostrou que
        # dois blurs eram 66% do tempo de cada frame (129 s para 16 s de
        # vídeo). Desenhar numa tela 1/6 e ampliar com LANCZOS dá o mesmo
        # resultado visual — é um brilho difuso, não tem detalhe fino — e é
        # 40x mais rápido.
        escala = 6
        lw, lh = max(1, width // escala), max(1, height // escala)
        brilho = Image.new("RGB", (lw, lh), (0, 0, 0))
        pincel = ImageDraw.Draw(brilho)
        for k in range(3):
            angulo = t * (0.6 + 0.25 * k) + k * math.tau / 3
            cx = lw * (0.5 + 0.28 * math.cos(angulo))
            cy = lh * (0.34 + 0.16 * math.sin(angulo * 1.3))
            raio = lw * (0.22 + 0.05 * math.sin(t * 1.7 + k))
            cor = tuple(min(255, int(c * (0.55 + 0.15 * k))) for c in acento)
            pincel.ellipse([cx - raio, cy - raio, cx + raio, cy + raio], fill=cor)
        brilho = brilho.filter(ImageFilter.GaussianBlur(lw / 9))
        brilho = brilho.resize((width, height), Image.BILINEAR)
        base = ImageChops.screen(base, brilho.point(lambda v: int(v * 0.6)))

        # Vinheta: sem ela o texto branco perde contraste nos cantos. Mesma
        # técnica — máscara em baixa resolução, ampliada.
        vinheta = Image.new("L", (lw, lh), 0)
        ImageDraw.Draw(vinheta).ellipse(
            [-lw * 0.2, -lh * 0.08, lw * 1.2, lh * 1.08], fill=255
        )
        vinheta = vinheta.filter(ImageFilter.GaussianBlur(lw / 10)).resize(
            (width, height), Image.BILINEAR
        )
        escuro = Image.new("RGB", (width, height), (4, 6, 14))
        base = Image.composite(base, escuro, vinheta)

        sobrepor = ImageDraw.Draw(base, "RGBA")

        # Título fixo em cima, pequeno: dá contexto sem competir com a fala.
        if titulo:
            fonte_titulo = _font(int(height * 0.026))
            sobrepor.text(
                (width / 2, height * 0.075), titulo.upper()[:46],
                font=fonte_titulo, fill=(255, 255, 255, 150), anchor="mm",
            )

        # A fala actual, grande e centrada — é o que se lê no telemóvel.
        if fala is not None:
            texto = fala.texto
            tamanho = int(height * 0.052)
            fonte = _font(tamanho)
            limite = int(width * 0.84)
            linhas = MotionRenderer._wrap(texto, fonte, limite)  # noqa: SLF001
            while len(linhas) > 4 and tamanho > int(height * 0.030):
                tamanho = int(tamanho * 0.9)
                fonte = _font(tamanho)
                linhas = MotionRenderer._wrap(texto, fonte, limite)  # noqa: SLF001
            altura_linha = int(tamanho * 1.3)
            y = int(height * 0.5) - (altura_linha * len(linhas)) // 2
            for linha in linhas:
                # Sombra: o gradiente pode passar por baixo do texto.
                sobrepor.text(
                    (width / 2 + 2, y + 2), linha, font=fonte,
                    fill=(0, 0, 0, 160), anchor="mt",
                )
                sobrepor.text(
                    (width / 2, y), linha, font=fonte,
                    fill=(255, 255, 255, 255), anchor="mt",
                )
                y += altura_linha

        # Barra de progresso: dá noção de fim e segura a atenção.
        sobrepor.rectangle(
            [0, height - 10, int(width * progresso), height],
            fill=(*acento, 255),
        )
        return base

    # -------------------------------------------------------------- montagem
    def render(
        self,
        *,
        falas: Sequence[Fala],
        titulo: str,
        out_dir: Path,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[int] = None,
    ) -> Tuple[Optional[Path], int, int, List[str]]:
        """Desenha os frames, junta com o áudio e escreve o MP4."""
        binario = self.renderer.ffmpeg()
        notas: List[str] = []
        if not binario:
            return None, 0, 0, ["ffmpeg indisponível"]
        if not falas:
            return None, 0, 0, ["sem falas sintetizadas"]

        w = int(width or settings.video_target_width)
        h = int(height or settings.video_target_height)
        taxa = int(fps or settings.video_target_fps)
        total_s = falas[-1].fim_s
        total_frames = max(1, int(round(total_s * taxa)))

        quintal = out_dir / "frames"
        quintal.mkdir(parents=True, exist_ok=True)
        inicio = time.time()

        # Fase por cena: a paleta muda quando muda a frase.
        indice_fala = 0
        for n in range(total_frames):
            momento = n / taxa
            while (
                indice_fala + 1 < len(falas)
                and momento >= falas[indice_fala].fim_s
            ):
                indice_fala += 1
            fala = falas[indice_fala] if momento <= falas[-1].fim_s else None
            imagem = self._frame(n, total_frames, fala, titulo, w, h, indice_fala)
            imagem.save(quintal / f"f{n:05d}.png", "PNG")
        ms_desenho = int((time.time() - inicio) * 1000)

        # Áudio: concatena as falas em sequência, com os mesmos silêncios
        # (nenhum) usados para medir os tempos do SRT.
        audio = out_dir / "narracao.wav"
        lista_audio = out_dir / "audio.txt"
        lista_audio.write_text(
            "\n".join(f"file '{f.wav.resolve().as_posix()}'" for f in falas),
            encoding="utf-8",
        )
        subprocess.run(
            [binario, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(lista_audio),
             "-c", "copy", str(audio)],
            capture_output=True, text=True, check=False,
        )
        if not audio.is_file():
            notas.append("concatenação do áudio falhou")

        video = out_dir / "submarino.mp4"
        comando = [
            binario, "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(taxa), "-i", str(quintal / "f%05d.png"),
        ]
        if audio.is_file():
            comando += ["-i", str(audio)]
        comando += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(taxa),
        ]
        if audio.is_file():
            comando += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
        comando += [str(video)]

        feito = subprocess.run(comando, capture_output=True, text=True, check=False)
        ms_render = int((time.time() - inicio) * 1000)
        if feito.returncode != 0 or not video.is_file():
            notas.append(
                f"ffmpeg falhou ({feito.returncode}): {(feito.stderr or '')[-300:]}"
            )
            return None, total_frames, ms_render, notas
        return video, total_frames, ms_render, notas

    # --------------------------------------------------------------- público
    def produzir(
        self,
        *,
        frases: Sequence[str],
        titulo: str,
        out_dir: Path,
        language: str = "pt",
    ) -> ResultadoOffline:
        """Faz tudo: voz, SRT, frames, MP4."""
        out_dir.mkdir(parents=True, exist_ok=True)
        resultado = ResultadoOffline(
            ok=False, fps=int(settings.video_target_fps),
            resolucao=f"{settings.video_target_width}x{settings.video_target_height}",
        )
        falas, motor, ms_tts = self.narrar(frases, out_dir, language=language)
        resultado.falas = [
            {"texto": f.texto, "inicio_s": round(f.inicio_s, 2),
             "duracao_s": round(f.duracao_s, 2)}
            for f in falas
        ]
        resultado.tts_engine = motor
        resultado.ms_tts = ms_tts
        if not falas:
            resultado.notas.append("nenhuma fala foi sintetizada")
            return resultado

        resultado.audio = out_dir / "narracao.wav"
        resultado.srt = self.escrever_srt(falas, out_dir / "legendas.srt")
        video, frames, ms_render, notas = self.render(
            falas=falas, titulo=titulo, out_dir=out_dir
        )
        resultado.frames = frames
        resultado.ms_render = ms_render
        resultado.notas.extend(notas)
        if video:
            resultado.ok = True
            resultado.video = video
            resultado.duracao_s = falas[-1].fim_s
        return resultado


offline_pipeline = OfflineVideoPipeline()
