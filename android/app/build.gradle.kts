plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

abstract class PrepareLauncherIcon : DefaultTask() {
    @get:InputFile
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val source: RegularFileProperty

    @get:OutputDirectory
    abstract val outputDirectory: DirectoryProperty

    @TaskAction
    fun copyIcon() {
        source.get().asFile.copyTo(
            outputDirectory.file("drawable-nodpi/component_library.png").get().asFile,
            overwrite = true,
        )
    }
}

android {
    namespace = "app.componentlibrary.mobile"
    compileSdk = 37

    defaultConfig {
        applicationId = "app.componentlibrary.mobile"
        minSdk = 26
        targetSdk = 37
        versionCode = 10
        versionName = "1.0.9"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildFeatures { compose = true }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

androidComponents {
    onVariants { variant ->
        val prepareIcon = tasks.register<PrepareLauncherIcon>("prepare${variant.name.replaceFirstChar { it.uppercase() }}LauncherIcon") {
            source.set(rootProject.layout.projectDirectory.file("../packaging/assets/icons/component-library.png"))
        }
        variant.sources.res?.addGeneratedSourceDirectory(prepareIcon, PrepareLauncherIcon::outputDirectory)
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2026.09.00")
    implementation(composeBom)
    androidTestImplementation(composeBom)
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.11.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.11.0")
    implementation("androidx.camera:camera-camera2:1.5.0")
    implementation("androidx.camera:camera-lifecycle:1.5.0")
    implementation("androidx.camera:camera-view:1.5.0")
    implementation("com.google.mlkit:barcode-scanning:17.3.0")
    implementation("com.squareup.okhttp3:okhttp:5.3.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20250517")
    testImplementation("com.squareup.okhttp3:mockwebserver:5.3.2")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
