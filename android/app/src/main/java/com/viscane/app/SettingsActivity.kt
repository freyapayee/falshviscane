package com.viscane.app

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.preference.EditTextPreference
import androidx.preference.Preference
import androidx.preference.PreferenceFragmentCompat

class SettingsActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportFragmentManager
            .beginTransaction()
            .replace(android.R.id.content, SettingsFragment())
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
                if (text.isBlank()) return@setOnPreferenceChangeListener false
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
