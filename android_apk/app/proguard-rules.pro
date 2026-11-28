# Regras ProGuard/R8 para o APK da holding.
#
# As regras oficiais de kotlinx.serialization (as que o R8 precisa para não
# apagar os serializers gerados em build de release) são estas — as três
# primeiras linhas substituem um conjunto anterior que só cobria o pacote
# kotlinx.serialization.json e deixava os `$serializer` da app ao critério do
# R8, o que parte o release com `SerializationException: Serializer for
# class ... is not found` apenas em produção.
-keepattributes *Annotation*, InnerClasses, Signature
-dontnote kotlinx.serialization.**

# Mantém o Companion e o serializer() de todas as classes @Serializable.
-keepclassmembers class **$$serializer {
    *** descriptor;
}
-keepclasseswithmembers class ** {
    kotlinx.serialization.KSerializer serializer(...);
}
-if @kotlinx.serialization.Serializable class **
-keepclassmembers class <1> {
    static <1>$Companion Companion;
}

# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**

# Biometria
-keep class androidx.biometric.** { *; }
