# Modelos locais

Conteúdo **não versionado** (107 MB) mas **reconstruível**. O `.gitignore`
exclui `*.onnx` e `*.npz`; o README fica.

| Ficheiro | Tamanho | Licença | Origem |
|---|---|---|---|
| `kokoro/kokoro-v1.0.q8.onnx` | 92,4 MB | Apache-2.0 | 6 fragmentos de `kokoro-q8-shards@1.0.0` (npm) |
| `kokoro/voices-v1.0.npz` | 14,6 MB | Apache-2.0 | 28 `.bin` de `kokoro-local-runtime@0.1.0` (npm) |

sha256 do modelo (verificado pelo script antes de aceitar):
`fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478`

```bash
bash scripts/fetch_models.sh     # idempotente, ~5 s, termina com uma síntese de controlo
```

Porque não vem do HuggingFace nem dos GitHub Releases: ambos estão bloqueados
nesta rede — ver `docs/stack_audit.md` §1. As vozes v1.0 cobrem apenas
inglês/chim; **português offline sai em espeak-ng** (ver §2 do mesmo documento).
