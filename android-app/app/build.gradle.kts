plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.example.pluginhost"
    compileSdk = 35

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    defaultConfig {
        applicationId = "com.example.pluginhost"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "com.example.pluginhost.Stage15Instrumentation"
    }

    buildFeatures {
        compose = true
    }

    // Install the same host code alongside the developer's app for Stage A.5 UI acceptance.
    if (providers.gradleProperty("stageA5Acceptance").orNull == "true") {
        buildTypes.getByName("debug").applicationIdSuffix = ".a5acceptance"
    }

    // Only the regression APK includes the temporary package matrix prepared by verify-stage15.py.
    providers.gradleProperty("stage15Assets").orNull?.let { directory ->
        sourceSets.getByName("androidTest").assets.srcDir(directory)
    }
    // Stage A.1 Android protocol checks consume the canonical repository fixtures.
    sourceSets.getByName("androidTest").assets.srcDir("../../message-protocol")
    sourceSets.getByName("androidTest").assets.srcDir("../../dialogue-protocol")
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation("androidx.activity:activity-compose:1.10.0")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.webkit:webkit:1.13.0")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
