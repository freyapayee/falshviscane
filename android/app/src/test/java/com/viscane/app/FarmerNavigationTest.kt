package com.viscane.app

import org.junit.Assert.*
import org.junit.Test

class FarmerNavigationTest {
    private val policy = FarmerNavigation("https://farmers.example.org/")

    @Test fun farmerRoutesAndAssetsAreAllowed() {
        for (path in listOf("/", "/auth?mode=register", "/homepage", "/api/scan/predict?top_k=3",
            "/farmer/cv-upload/17/delete", "/farmer/cv-upload/17/image", "/static/style.css", "/media/photo.jpg")) {
            assertTrue(path, policy.allows("https://farmers.example.org$path"))
        }
    }

    @Test fun administratorAndUntrustedDestinationsAreBlocked() {
        for (url in listOf("https://farmers.example.org/admin", "https://farmers.example.org/admin-login",
            "https://farmers.example.org/superadmin", "https://farmers.example.org/%61dmin",
            "https://farmers.example.org/static/../admin", "https://farmers.example.org.evil.org/homepage",
            "https://farmers.example.org@evil.org/homepage", "http://farmers.example.org/homepage",
            "https://farmers.example.org:444/homepage", "file:///homepage", "javascript:alert(1)")) {
            assertFalse(url, policy.allows(url))
        }
    }

    @Test fun onlyDebugCanUseHttpOrigins() {
        assertTrue(FarmerNavigation.validOrigin("http://10.0.2.2:5000/", true))
        assertFalse(FarmerNavigation.validOrigin("http://10.0.2.2:5000/", false))
        assertFalse(FarmerNavigation.validOrigin("https://example.org/admin", false))
    }
}
