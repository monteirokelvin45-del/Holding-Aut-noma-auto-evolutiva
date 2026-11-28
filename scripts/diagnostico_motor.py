"""Diagnóstico do motor local, para o runner onde os logs não são legíveis.

Existe porque o blob de resultados do Actions (`productionresultssa*.blob.
core.windows.net`) devolve EOF a quem tenta ler os logs de fora: sem este
ficheiro, uma run que diz `local:used` sem o provider local ser tentado não
distingue entre «o pacote não instalou», «o GGUF não existe» e «ficou fora da
ordem». Escreve tudo em `motor_local.txt`, que o workflow publica no ramo
`estado-ciclo`.
"""

from __future__ import annotations

import sys
from pathlib import Path

PACOTES = (
    "llama_cpp",
    "misaki",
    "onnxruntime",
    "phonemizer",
    "espeakng_loader",
    "soundfile",
)


def linha(titulo: str) -> None:
    print(f"\n== {titulo}")


def main() -> int:
    linha("python")
    print(sys.version.split()[0], sys.executable)

    linha("pacotes")
    for nome in PACOTES:
        try:
            modulo = __import__(nome)
            versao = getattr(modulo, "__version__", "ok")
            print(f"  {nome:18} {versao}")
        except Exception as exc:
            print(f"  {nome:18} AUSENTE ({type(exc).__name__}: {exc})")

    linha("ficheiros")
    casa = Path.home()
    for pasta in (casa / "modelos", Path("models/kokoro")):
        print(f"  {pasta}: {'existe' if pasta.exists() else 'NÃO EXISTE'}")
        if pasta.is_dir():
            for f in sorted(pasta.iterdir())[:6]:
                marca = f"{f.stat().st_size} bytes" if f.is_file() else "dir"
                print(f"    {f.name} — {marca}")
    vozes = Path("models/kokoro/voices")
    if vozes.is_dir():
        print(f"  vozes: {len(list(vozes.glob('*.bin')))} ficheiros .bin")

    linha("providers segundo o router")
    try:
        from backend.core.llm_router import llm_router

        estado = llm_router.status()
        print("  ordem:", estado["order"])
        for nome, info in estado["providers"].items():
            print(f"  {nome:14} configurado={info['configured']!s:5} {info['reason']}")
    except Exception as exc:
        print(f"  ERRO: {type(exc).__name__}: {exc}")

    linha("motor GGUF")
    try:
        from backend.config.settings import settings

        caminho = Path(settings.local_gguf_path or "")
        print(f"  LOCAL_GGUF_PATH = {settings.local_gguf_path or '(vazio)'}")
        print(f"  existe: {caminho.is_file() if settings.local_gguf_path else False}")
        if caminho.is_file():
            print(f"  tamanho: {caminho.stat().st_size} bytes")
    except Exception as exc:
        print(f"  ERRO: {type(exc).__name__}: {exc}")

    linha("Kokoro")
    try:
        from backend.squads.kokoro_tts import kokoro_local

        ok, motivo = kokoro_local.disponivel()
        print(f"  disponivel: {ok} — {motivo}")
    except Exception as exc:
        print(f"  ERRO: {type(exc).__name__}: {exc}")

    linha("TTS disponíveis")
    try:
        from backend.squads.video_factory_squad import TTSRegistry

        for motor, ok in TTSRegistry().available().items():
            print(f"  {motor:12} {ok}")
    except Exception as exc:
        print(f"  ERRO: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
