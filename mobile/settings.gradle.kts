pluginManagement {
    repositories {
        System.getenv("LOCAL_ANDROID_MAVEN")?.let { maven { url = uri(it) } }
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        System.getenv("LOCAL_ANDROID_MAVEN")?.let { maven { url = uri(it) } }
        google()
        mavenCentral()
    }
}

rootProject.name = "ComponentLibraryMobile"
include(":app")
