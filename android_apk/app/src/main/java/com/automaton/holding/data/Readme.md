# Camada de dados do APK

- `ApiService.kt` — cliente REST + WebSocket (OkHttp + kotlinx.serialization).
  - `BASE_URL` e `API_TOKEN` chegam por `BuildConfig` (ver `app/build.gradle.kts`).
  - Todos os endpoints de escrita exigem o header `X-Automaton-Token` quando
    `API_ACCESS_TOKEN` está definido no backend.
  - O stream `liveEvents()` liga-se a `/ws/live` e reconecta com backoff.
- `BiometricAuthManager.kt` — `BiometricPrompt` com Classe 3 (forte) quando
  disponível. Devolve `SUCCESS` apenas após autenticação real; o backend
  recusa decisões financeiras sem `biometric_verified=true`.

Modelos espelham exactamente os contratos do FastAPI; campos desconhecidos são
ignorados (`ignoreUnknownKeys = true`) para permitir evoluir o backend sem
partir APKs antigos.
