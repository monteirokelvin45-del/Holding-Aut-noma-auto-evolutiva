#!/usr/bin/env python3
"""Reconstrói o cérebro local (Qwen3-0.6B GGUF) de forma reprodutível.

Porquê este script
------------------
O sandbox reinicia e leva consigo o venv e os 397 MB de pesos. Reconstruir
isso à mão, à pressa, já custou uma sessão inteira. Este script faz as quatro
coisas em sequência e é **idempotente**: o que já existe e verifica não é
descarregado outra vez.

  1. junta as 5 partes de `tripilot-model-qwen` vindas do registry npm;
  2. confere o SHA-256 contra o declarado no `provenance.json` do pacote
     (não contra um valor escrito à mão neste ficheiro);
  3. instala `llama-cpp-python` no venv indicado, se faltar;
  4. faz uma inferência real e imprime latência e tokens/s.

Porque não Ollama
-----------------
`ollama.com`, `registry.ollama.ai` e `release-assets.githubusercontent.com`
devolvem 000 neste ambiente. O `llama-cpp-python` abre o `.gguf` directamente
e não precisa de servidor nenhum.

Uso
---
    python3 scripts/ensure_local_llm.py                 # tudo
    python3 scripts/ensure_local_llm.py --so-pesos      # sem pip, sem inferência
    python3 scripts/ensure_local_llm.py --destino /out  # outro sítio

Códigos de saída: 0 = pronto e verificado; 1 = falhou (diz onde).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

REGISTRO = "https://registry.npmjs.org"
MANIFESTO = "tripilot-model-qwen"
VERSao = "0.1.0-alpha.1"
PARTES = [f"{MANIFESTO}-part-{i}" for i in range(1, 6)]
NOME_GGUF = "Qwen3-0.6B-Q4_K_M.gguf"


def _tarball(pacote: str, versao: str = VERSao) -> str:
    return f"{REGISTRO}/{pacote}/-/{pacote}-{versao}.tgz"


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _descarregar(url: str, destino: Path) -> None:
    pedido = urllib.request.Request(url, headers={"User-Agent": "automaton-local-llm"})
    with urllib.request.urlopen(pedido, timeout=120) as resposta, destino.open("wb") as f:
        while True:
            bloco = resposta.read(1 << 20)
            if not bloco:
                break
            f.write(bloco)


def _extrair_partes(tgz: Path, destino: Path) -> Path:
    """Extrai `package/artifacts/weights.part` de um tarball npm."""
    with tarfile.open(tgz) as tar:
        for membro in tar.getmembers():
            if membro.name.endswith("artifacts/weights.part"):
                extraido = tar.extractfile(membro)
                if extraido is None:
                    raise RuntimeError(f"{tgz.name}: weights.part ilegível")
                destino.write_bytes(extraido.read())
                return destino
    raise RuntimeError(f"{tgz.name}: não contém artifacts/weights.part")


def _provenance() -> dict:
    """Lê a proveniência declarada pelo pacote (fonte da verdade do SHA)."""
    with tempfile.TemporaryDirectory() as tmp:
        tgz = Path(tmp) / "manifesto.tgz"
        _descarregar(_tarball(MANIFESTO), tgz)
        with tarfile.open(tgz) as tar:
            for membro in tar.getmembers():
                if membro.name.endswith("provenance.json"):
                    extraido = tar.extractfile(membro)
                    if extraido is None:
                        break
                    return json.loads(extraido.read().decode("utf-8"))
    raise RuntimeError("provenance.json não encontrado no pacote")


def garantir_pesos(destino: Path, forcar: bool = False) -> Path:
    gguf = destino / NOME_GGUF
    destino.mkdir(parents=True, exist_ok=True)

    print("== proveniência")
    prov = _provenance()
    modelo = prov.get("model") or {}
    esperado = str(modelo.get("sha256") or "")
    bytes_esperados = int(modelo.get("bytes") or 0)
    origem = str(modelo.get("source") or "?")
    print(f"   origem HF: {origem[:90]}")
    print(f"   bytes declarados: {bytes_esperados}")
    print(f"   sha256 declarado: {esperado}")
    if not esperado:
        raise RuntimeError("provenance.json sem sha256 — não confio no ficheiro")

    if gguf.is_file() and not forcar:
        obtido = _sha256(gguf)
        if obtido == esperado and (not bytes_esperados or gguf.stat().st_size == bytes_esperados):
            print(f"== pesos já presentes e verificados ({gguf.stat().st_size} bytes)")
            return gguf
        print("== pesos presentes mas não conferem; a reconstruir")

    print("== a descarregar 5 fragmentos")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        partes: list[Path] = []
        for nome in PARTES:
            tgz = tmp / f"{nome}.tgz"
            print(f"   {nome}")
            _descarregar(_tarball(nome), tgz)
            partes.append(_extrair_partes(tgz, tmp / f"{nome}.part"))

        print("== a juntar")
        with gguf.open("wb") as saida:
            for parte in partes:
                with parte.open("rb") as f:
                    for bloco in iter(lambda: f.read(1 << 22), b""):
                        saida.write(bloco)

    obtido = _sha256(gguf)
    tamanho = gguf.stat().st_size
    print(f"== verificação\n   bytes: {tamanho}\n   sha256: {obtido}")
    if obtido != esperado:
        gguf.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA-256 não confere (esperado {esperado}). Ficheiro apagado."
        )
    if bytes_esperados and tamanho != bytes_esperados:
        raise RuntimeError(f"tamanho {tamanho} != declarado {bytes_esperados}")
    print("   OK — confere com o provenance.json")
    return gguf


def garantir_llama_cpp(python: str) -> None:
    teste = subprocess.run(
        [python, "-c", "import llama_cpp; print(llama_cpp.__version__)"],
        capture_output=True, text=True,
    )
    if teste.returncode == 0:
        print(f"== llama-cpp-python já instalado ({teste.stdout.strip()})")
        return
    print("== a instalar llama-cpp-python (compila do sdist; ~13 min em 2 núcleos)")
    inicio = time.time()
    inst = subprocess.run(
        [python, "-m", "pip", "install", "--quiet", "llama-cpp-python"],
        capture_output=True, text=True,
    )
    if inst.returncode != 0:
        raise RuntimeError(
            "pip install llama-cpp-python falhou:\n" + (inst.stderr or "")[-800:]
        )
    print(f"   instalado em {time.time() - inicio:.0f}s")


def testar_inferencia(gguf: Path) -> None:
    print("== inferência real")
    from llama_cpp import Llama  # type: ignore

    t0 = time.time()
    modelo = Llama(model_path=str(gguf), n_ctx=2048, n_threads=2, verbose=False)
    print(f"   carregar: {(time.time() - t0) * 1000:.0f} ms")

    pergunta = (
        "Em uma frase, qual é a maior dificuldade de um freelancer "
        "em Maputo a começar hoje?"
    )
    t1 = time.time()
    saida = modelo.create_chat_completion(
        messages=[
            {"role": "system", "content": "/no_think Responde em português, direto."},
            {"role": "user", "content": pergunta},
        ],
        max_tokens=220,
        temperature=0.4,
    )
    ms = (time.time() - t1) * 1000
    uso = saida.get("usage") or {}
    tokens = int(uso.get("completion_tokens") or 0)
    texto = (saida["choices"][0]["message"]["content"] or "").strip()
    print(f"   latência: {ms:.0f} ms | tokens: {tokens} | {tokens / (ms / 1000):.1f} tok/s")
    print(f"   resposta: {texto[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destino", default=os.environ.get("LOCAL_MODELS_DIR", str(Path.home() / "modelos"))
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--so-pesos", action="store_true")
    parser.add_argument("--forcar", action="store_true")
    args = parser.parse_args()

    destino = Path(args.destino).expanduser()
    try:
        gguf = garantir_pesos(destino, forcar=args.forcar)
        if args.so_pesos:
            print(f"\nGGUF pronto: {gguf}")
            return 0
        garantir_llama_cpp(args.python)
        testar_inferencia(gguf)
    except Exception as exc:
        print(f"\nERRO: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"\nPara usar:\n  export LOCAL_GGUF_PATH={gguf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
