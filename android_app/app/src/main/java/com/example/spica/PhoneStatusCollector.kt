package com.example.spica

import android.app.AppOpsManager
import android.app.usage.UsageStats
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.provider.Settings
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

class PhoneStatusCollector(private val context: Context) {
    fun deviceId(): String {
        return Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            ?: Build.MODEL
    }

    fun buildEvent(nowMs: Long = System.currentTimeMillis()): JSONObject {
        return JSONObject()
            .put("event_id", UUID.randomUUID().toString())
            .put("occurred_at_ms", nowMs)
            .put("collected_at_ms", nowMs)
            .put("type", "status")
            .put("snapshot", snapshot(nowMs))
    }

    private fun snapshot(nowMs: Long): JSONObject {
        val battery = batterySnapshot()
        val usageGranted = hasUsageAccess()
        val recentApps = if (usageGranted) recentApps(nowMs) else JSONArray()
        val foreground = recentApps.optJSONObject(0)

        return JSONObject()
            .put("manufacturer", Build.MANUFACTURER)
            .put("model", Build.MODEL)
            .put("android_release", Build.VERSION.RELEASE)
            .put("sdk_int", Build.VERSION.SDK_INT)
            .put("app_version", appVersion())
            .put("battery_percent", battery.first)
            .put("is_charging", battery.second)
            .put("network_type", networkType())
            .put("usage_access_granted", usageGranted)
            .put("foreground_app", foreground ?: JSONObject.NULL)
            .put("recent_apps", recentApps)
    }

    private fun batterySnapshot(): Pair<Int, Boolean> {
        val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val status = intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val percent = if (level >= 0 && scale > 0) (level * 100 / scale) else -1
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
            status == BatteryManager.BATTERY_STATUS_FULL
        return percent to charging
    }

    private fun networkType(): String {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val network = manager.activeNetwork ?: return "none"
        val capabilities = manager.getNetworkCapabilities(network) ?: return "unknown"
        return when {
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            else -> "unknown"
        }
    }

    fun hasUsageAccess(): Boolean {
        val appOps = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = appOps.checkOpNoThrow(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            android.os.Process.myUid(),
            context.packageName,
        )
        return mode == AppOpsManager.MODE_ALLOWED
    }

    private fun recentApps(nowMs: Long): JSONArray {
        val manager = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val start = nowMs - 30L * 60L * 1000L
        val stats = manager.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, start, nowMs)
            .orEmpty()
            .filter { it.lastTimeUsed >= start || it.totalTimeInForeground > 0 }
            .sortedWith(compareByDescending<UsageStats> { it.lastTimeUsed }
                .thenByDescending { it.totalTimeInForeground })
            .take(5)

        val array = JSONArray()
        stats.forEach { stat ->
            array.put(
                JSONObject()
                    .put("package_name", stat.packageName)
                    .put("app_name", labelFor(stat.packageName))
                    .put("total_time_ms", stat.totalTimeInForeground)
                    .put("last_time_used_ms", stat.lastTimeUsed)
            )
        }
        return array
    }

    private fun labelFor(packageName: String): String {
        return runCatching {
            val info = context.packageManager.getApplicationInfo(packageName, 0)
            context.packageManager.getApplicationLabel(info).toString()
        }.getOrDefault(packageName)
    }

    private fun appVersion(): String {
        return runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                context.packageManager.getPackageInfo(
                    context.packageName,
                    PackageManager.PackageInfoFlags.of(0),
                ).versionName
            } else {
                @Suppress("DEPRECATION")
                context.packageManager.getPackageInfo(context.packageName, 0).versionName
            }
        }.getOrNull() ?: "unknown"
    }
}
