package com.viscane.app

import android.os.Bundle
import android.widget.FrameLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.preference.EditTextPreference
import androidx.preference.Preference
import androidx.preference.PreferenceFragmentCompat

class SettingsActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!BuildConfig.DEBUG) { finish(); return }
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        val container = FrameLayout(this).apply { id = R.id.settings_container }
        setContentView(container)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        ViewCompat.setOnApplyWindowInsetsListener(container) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val keyboard = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, keyboard.bottom))
            insets
        }
        if (savedInstanceState != null) return
        supportFragmentManager
            .beginTransaction()
            .replace(container.id, SettingsFragment())
            .commit()
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    class SettingsFragment : PreferenceFragmentCompat() {
        override fun onCreatePreferences(savedInstanceState: Bundle?, rootKey: String?) {
            setPreferencesFromResource(R.xml.preferences, rootKey)

            val baseUrlPref = findPreference<EditTextPreference>(KEY_BASE_URL)
            baseUrlPref?.summaryProvider = Preference.SummaryProvider<EditTextPreference> { pref ->
                val value = pref.text?.trim().orEmpty()
                if (value.isBlank()) "Not set" else value
            }
            baseUrlPref?.setOnPreferenceChangeListener { _, newValue ->
                val text = (newValue as? String).orEmpty().trim()
                if (!FarmerNavigation.validOrigin(text, true)) return@setOnPreferenceChangeListener false
                val normalized = if (text.endsWith("/")) text else "$text/"
                if (normalized != baseUrlPref?.text) {
                    baseUrlPref?.text = normalized
                }
                false
            }
        }
    }

    companion object {
        const val KEY_BASE_URL = "base_url"
        const val DEFAULT_BASE_URL = "http://10.0.2.2:5000/"
    }
}
