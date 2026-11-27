# Regras ProGuard/R8 para o APK da holding.
# kotlinx.serialization: manter serializers gerados.
-keepattributes *Annotation*, InnerClasses, Signature
-keepclassmembers class kotlinx.serialization.json.** { *** Companion; }
-keepclasseswithmembers class kotlinx.serialization.json.** { kotlinx.serialization.KSerializer serializer(...); }
-keep,includedescriptorclasses class com.automaton.holding.**$$serializer { *; }
-keepclassmembers class com.automaton.holding.** { *** Companion; }
-keepclasseswithmembers class com.automaton.holding.** { kotlinx.serialization.KSerializer serializer(...); }

# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**

# Biometric
-keep class androidx.biometric.** { *; }
