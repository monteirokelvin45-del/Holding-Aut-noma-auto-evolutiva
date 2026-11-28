"""Kokoro-82M real, offline, com vozes portuguesas — sem HuggingFace.

Porquê este módulo
------------------
O código anterior desistia do Kokoro em português com uma justificação errada:
«o pacote voices-v1.0 só tem vozes en/zh». É falso. O Kokoro-82M v1.0 tem 54
vozes, incluindo português:

    pf_dora, pm_alex, pm_santa   (feminina / masculina / masculina)

O que faltava não era a voz — era ter os ficheiros. Neste ambiente
`huggingface.co` devolve 000 (bloqueado), por isso os pesos vieram por outra
via, com integridade verificada:

  modelo  kokoro-q8-shards (npm)   6 fragmentos → 92361116 bytes
          sha256 fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478
  vozes   kokoro-js (npm)          54 *.bin de 522240 bytes (510 × 256 f32)

O que este módulo faz que a lib `kokoro_onnx` não faz
-----------------------------------------------------
A `kokoro_onnx` recusa português (só en/zh) e o `misaki` não traz G2P de
português. Aqui:

  texto → espeak-ng pt-br (via `misaki.espeak.EspeakG2P`) → fonemas IPA
        → vocabulário do Kokoro → input_ids
        → style = linha `len(input_ids)` da voz
        → onnxruntime → waveform 24 kHz

É o mesmo caminho que a referência (kokoro-onnx) usa para inglês, aplicado ao
português com o espeak como fonemizador. A qualidade é a do modelo; a pronúncia
é a do espeak pt-br, que é a variante brasileira — há diferenças de sotaque em
relação a Moçambique (o «-s» final, a nasalidade), e isso fica declarado em vez
de escondido.

Toolchain de fonemas
--------------------
`misaki` precisa de `phonemizer`, que precisa da biblioteca `libespeak-ng.so`.
O pacote `espeakng_loader` (já em `requirements.txt`) traz essa biblioteca e
`TTSRegistry._configure_phonemizer()` aponta o `PHONEMIZER_ESPEAK_LIBRARY` para
ela. Sem isso, o espeak não arranca e este motor cai para `espeak_ng` puro.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config.settings import settings
from ..core.logger import get_logger

log = get_logger("squads.kokoro_local")

#: Vocabulário oficial do Kokoro-82M (styletts2), pela ordem do config.
#: Índice = id do token no `input_ids`. Fonte: config.json do
#: onnx-community/Kokoro-82M-v1.0-ONNX (Apache-2.0).
VOCAB: Dict[str, int] = {
    c: i
    for i, c in enumerate(
        '0;:,.!?—…"()\u201c\u201d \u0303\u02a3\u1d4a\u02a4\u1d67\u02a6\u1d67'
        "\u02a7\u02c8\u02cc"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "ɑɐɒæβɔɕçɐðəɚɛɝɞɡɥɨɪʝɯɫɬɭɲɳɴŋɹɻʁɾɿʂʃʈʇʊʋʌɣɤχʒʔʰʲʲ"
        "β̞̩̩̻βθχᵻ"
    )
}

#: Ditongos e africadas do espeak que o Kokoro representa com um só símbolo.
#: É o mesmo mapa de `misaki.espeak.EspeakG2P.e2m`.
E2M = (
    ("a^ɪ", "I"), ("a^ʊ", "W"), ("d^z", "ʣ"), ("d^ʒ", "ʤ"), ("e^ɪ", "A"),
    ("o^ʊ", "O"), ("ə^ʊ", "Q"), ("s^s", "S"), ("t^s", "ʦ"), ("t^ʃ", "ʧ"),
    ("ɔ^ɪ", "Y"),
)

#: Vozes portuguesas do pacote v1.0.
VOZES_PT = {"feminina": "pf_dora", "masculina": "pm_alex", "masculina2": "pm_santa"}

TAXA_AMOSTRAGEM = 24000
DIM_STYLE = 256


class KokoroLocalIndisponivel(RuntimeError):
    """Falta o modelo, as vozes ou o fonemizador."""


class KokoroLocal:
    """Síntese Kokoro-82M offline. Carrega o ONNX uma vez e reutiliza."""

    def __init__(
        self,
        modelo: Optional[Path] = None,
        vozes_dir: Optional[Path] = None,
    ) -> None:
        self.modelo = Path(modelo or settings.kokoro_modelo_onnx)
        self.vozes_dir = Path(vozes_dir or settings.kokoro_vozes_dir)
        self._sessao = None
        self._g2p = None
        self._cache_vozes: Dict[str, "object"] = {}

    # ------------------------------------------------------------------ estado
    def disponivel(self) -> Tuple[bool, str]:
        """Diz se pode sintetizar, e porquê, sem carregar o modelo."""
        if not self.modelo.is_file():
            return False, f"modelo ausente ({self.modelo})"
        if not self.vozes_dir.is_dir():
            return False, f"vozes ausentes ({self.vozes_dir})"
        if not (self.vozes_dir / f"{VOZES_PT['masculina']}.bin").is_file():
            return False, f"voz pm_alex ausente em {self.vozes_dir}"
        import importlib.util

        for modulo in ("onnxruntime", "numpy", "misaki", "phonemizer"):
            if importlib.util.find_spec(modulo) is None:
                return False, f"falta {modulo}"
        return True, "pronto"

    # ------------------------------------------------------------- carregamento
    def _carregar(self) -> None:
        if self._sessao is not None:
            return
        ok, motivo = self.disponivel()
        if not ok:
            raise KokoroLocalIndisponivel(motivo)
        import onnxruntime as ort  # type: ignore

        self._sessao = ort.InferenceSession(
            str(self.modelo), providers=["CPUExecutionProvider"]
        )
        log.warning("Kokoro-82M carregado de %s", self.modelo)

    def _fonemas(self, texto: str) -> str:
        """Texto → IPA, com o espeak-ng de português por dentro do misaki."""
        if self._g2p is None:
            # A lib do espeak vem do espeakng_loader; o TTSRegistry sabe onde a
            # pôr antes de o phonemizer arrancar.
            try:
                from .video_factory_squad import TTSRegistry

                TTSRegistry._configure_phonemizer()  # noqa: SLF001
            except Exception as exc:  # pragma: no cover - depende do sistema
                log.info("phonemizer já configurado (%s)", exc)
            from misaki.espeak import EspeakG2P  # type: ignore

            self._g2p = EspeakG2P(language=settings.kokoro_idioma_espeak)
        fonemas, _ = self._g2p(texto)
        for antigo, novo in E2M:
            fonemas = fonemas.replace(antigo.replace("^", "^"), novo)
        return fonemas.replace("^", "")

    def _ids(self, fonemas: str) -> List[int]:
        ids: List[int] = []
        ignorados = set()
        for char in fonemas:
            if char in VOCAB:
                ids.append(VOCAB[char])
            else:
                ignorados.add(char)
        if ignorados:
            log.info("Kokoro: símbolos sem token ignorados: %r", sorted(ignorados))
        return ids

    def _style(self, voz: str, posicao: int):
        import numpy as np  # type: ignore

        caminho = self.vozes_dir / f"{voz}.bin"
        if voz not in self._cache_vozes:
            dados = np.fromfile(caminho, dtype=np.float32)
            if dados.size % DIM_STYLE:
                raise KokoroLocalIndisponivel(
                    f"{caminho} não é múltiplo de {DIM_STYLE} floats"
                )
            self._cache_vozes[voz] = dados.reshape(-1, DIM_STYLE)
        linhas = self._cache_vozes[voz]
        # A linha de style corresponde ao comprimento do texto: vozes curtas
        # usam as primeiras linhas. `min(len-1)` evita estourar em textos
        # longos (as vozes têm 510 linhas).
        indice = min(max(0, posicao - 1), len(linhas) - 1)
        return linhas[indice]

    # ---------------------------------------------------------------- síntese
    def sintetizar(
        self, texto: str, saida: Path, voz: str = "", velocidade: float = 1.0
    ) -> Tuple[bool, str]:
        """Gera um WAV mono 24 kHz. Devolve ``(ok, motivo)``."""
        texto = (texto or "").strip()
        if not texto:
            return False, "kokoro: texto vazio"
        try:
            self._carregar()
            fonemas = self._fonemas(texto)
            ids = self._ids(fonemas)
            if not ids:
                return False, f"kokoro: nenhum fonema mapeado ({fonemas[:60]!r})"
            import numpy as np  # type: ignore

            estilo = self._style(voz or VOZES_PT["feminina"], len(ids))
            audio = self._sessao.run(
                None,
                {
                    "input_ids": np.array([ids], dtype=np.int64),
                    "style": np.asarray(estilo, dtype=np.float32).reshape(1, DIM_STYLE),
                    "speed": np.array([velocidade], dtype=np.float32),
                },
            )[0][0]
        except KokoroLocalIndisponivel as exc:
            return False, f"kokoro:{exc}"
        except Exception as exc:  # ONNX, fonemas, memória
            return False, f"kokoro:{type(exc).__name__}: {exc}"

        saida.parent.mkdir(parents=True, exist_ok=True)
        pcm = np.clip(audio, -1.0, 1.0)
        with wave.open(str(saida), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TAXA_AMOSTRAGEM)
            w.writeframes((pcm * 32767).astype("<i2").tobytes())
        duracao = len(pcm) / TAXA_AMOSTRAGEM
        if duracao < 0.1:
            return False, f"kokoro:audio_curto ({duracao:.2f}s)"
        return True, f"kokoro:{voz or VOZES_PT['feminina']}:{duracao:.1f}s"


kokoro_local = KokoroLocal()
