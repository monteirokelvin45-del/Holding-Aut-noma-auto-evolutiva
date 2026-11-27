// Configuração do módulo :app — Kotlin 2.0 + Jetpack Compose (Material 3).
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "com.automaton.holding"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.automaton.holding"
        minSdk = 26            // BiometricPrompt + WebSocket nativo
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"

        // URL do backend injectada em tempo de build (BuildConfig).
        //   -Pautomaton.baseUrl=...  ou  AUTOMATON_BASE_URL no ambiente.
        //
        // Sem valor explícito, o build de **debug** aponta para
        // http://10.0.2.2:8000 — o alias documentado do emulador Android para
        // a máquina anfitriã. Não é um domínio inventado: é o endereço do
        // backend local. O build de **release** exige a URL explícita (ver
        // buildTypes abaixo), porque um APK de produção a apontar para o
        // emulador seria inútil.
        // Antes o valor por omissão era https://automaton-holding.fly.dev,
        // que não corresponde a serviço nenhum.
        val urlExplicita = (project.findProperty("automaton.baseUrl") as String?)
            ?: System.getenv("AUTOMATON_BASE_URL")
        val baseUrl = urlExplicita ?: "http://10.0.2.2:8000"
        buildConfigField("String", "BASE_URL", "\"$baseUrl\"")

        // Token do APK (header X-Automaton-Token). Nunca commitar valores reais:
        // passar via -Pautomaton.token=... ou AUTOMATON_API_TOKEN no ambiente.
        val apiToken = (project.findProperty("automaton.token") as String?)
            ?: System.getenv("AUTOMATON_API_TOKEN")
            ?: ""
        buildConfigField("String", "API_TOKEN", "\"$apiToken\"")
    }

    buildTypes {
        release {
            // Um release a apontar para o emulador (ou sem URL) não serve.
            if (urlExplicita.isNullOrBlank()) {
                throw GradleException(
                    "Build de release sem URL do backend: passe " +
                        "-Pautomaton.baseUrl=https://... ou defina AUTOMATON_BASE_URL."
                )
            }
            buildConfigField("String", "BASE_URL", "\"$urlExplicita\"")
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // Assinatura: configurada por variáveis de ambiente no CI.
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
        debug {
            isMinifyEnabled = false
            applicationIdSuffix = ".debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources {
            excludes += setOf(
                "/META-INF/{AL2.0,LGPL2.1}",
                "META-INF/DEPENDENCIES",
                "META-INF/LICENSE*"
            )
        }
    }

    testOptions {
        // Os testes de contrato correm na JVM; android.util.Log não existe lá.
        unitTests.isReturnDefaultValues = true
    }

    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.11.00")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.activity:activity-compose:1.9.3")
    // FragmentActivity: necessária para o BiometricPrompt (aprovações).
    implementation("androidx.fragment:fragment-ktx:1.8.5")

    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // Biometria (aprovação de gastos e saques).
    // NOTA: a linha "1.2.0-alpha05" não corresponde a nenhum artefacto
    // publicado — as versões reais da 1.2.x são 1.2.0-alpha01..alpha06.
    // 1.1.0 é a estável mais recente e tem tudo o que usamos
    // (BIOMETRIC_STRONG/WEAK, setAllowedAuthenticators).
    implementation("androidx.biometric:biometric:1.1.0")

    // Rede: OkHttp (REST + WebSocket) e kotlinx.serialization
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
}
