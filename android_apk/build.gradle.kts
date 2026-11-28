// Build script raiz do APK Android (Gradle 8.x / AGP 8.7).
// Todas as versões estão fixadas para builds reprodutíveis no CI.
plugins {
    id("com.android.application") version "8.7.2" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
    // Sem este plugin os 16 @Serializable de ApiService.kt não geram serializer
    // e o build falha com "Serializer for class X is not found".
    id("org.jetbrains.kotlin.plugin.serialization") version "2.0.21" apply false
}
