package com.example.spica

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import com.example.spica.ui.theme.SpicaTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            SpicaTheme {
                SpicaApp()
            }
        }
    }
}

@Composable
fun SpicaApp() {
    val context = LocalContext.current
    val preferences = remember { context.getSharedPreferences("bridge_config", Context.MODE_PRIVATE) }
    val scope = rememberCoroutineScope()
    val collector = remember { PhoneStatusCollector(context) }
    val store = remember { PhoneEventStore(context) }

    var bridgeUrl by remember {
        mutableStateOf(preferences.getString("bridge_url", "http://192.168.1.100:8765") ?: "")
    }
    var token by remember { mutableStateOf(preferences.getString("token", "") ?: "") }
    var autoSend by remember { mutableStateOf(preferences.getBoolean("auto_send", false)) }
    var statusText by remember { mutableStateOf("Ready") }
    var pendingCount by remember { mutableIntStateOf(store.count()) }
    var isSending by remember { mutableStateOf(false) }
    val usageGranted = collector.hasUsageAccess()

    fun saveConfig() {
        preferences.edit()
            .putString("bridge_url", bridgeUrl)
            .putString("token", token)
            .putBoolean("auto_send", autoSend)
            .apply()
    }

    suspend fun collectAndSendOnce() {
        isSending = true
        val result = submitPhoneStatus(context, bridgeUrl, token)
        pendingCount = store.count()
        statusText = result.message
        isSending = false
    }

    LaunchedEffect(autoSend, bridgeUrl, token) {
        var failureCount = 0
        while (autoSend) {
            val result = submitPhoneStatus(context, bridgeUrl, token)
            pendingCount = store.count()
            statusText = result.message
            failureCount = if (result.success) 0 else failureCount + 1
            delay(if (result.success) 60_000L else nextBackoffMs(failureCount))
        }
    }

    Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
        Column(
            modifier = Modifier
                .padding(innerPadding)
                .padding(20.dp)
                .fillMaxSize()
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text("Spica Companion", style = MaterialTheme.typography.headlineSmall)
            Text("Device ID: ${collector.deviceId()}", style = MaterialTheme.typography.bodyMedium)
            Text(
                "Usage Access: ${if (usageGranted) "Granted" else "Not granted"}",
                style = MaterialTheme.typography.bodyMedium,
            )

            OutlinedTextField(
                value = bridgeUrl,
                onValueChange = { bridgeUrl = it },
                label = { Text("Bridge URL") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("Shared token") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                modifier = Modifier.fillMaxWidth(),
            )

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("Auto send while foreground")
                Switch(
                    checked = autoSend,
                    onCheckedChange = {
                        autoSend = it
                        saveConfig()
                    },
                )
            }

            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    enabled = !isSending,
                    onClick = {
                        saveConfig()
                        scope.launch { collectAndSendOnce() }
                    },
                ) {
                    Text(if (isSending) "Sending" else "Send now")
                }
                Button(onClick = { saveConfig(); statusText = "Saved" }) {
                    Text("Save")
                }
            }

            Button(
                onClick = {
                    context.startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
                },
            ) {
                Text("Open Usage Access")
            }

            Spacer(Modifier.height(10.dp))
            Text("Pending events: $pendingCount", style = MaterialTheme.typography.titleMedium)
            Text(statusText, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

data class SubmitStatusResult(
    val success: Boolean,
    val message: String,
)

suspend fun submitPhoneStatus(
    context: Context,
    bridgeUrl: String,
    token: String,
): SubmitStatusResult = withContext(Dispatchers.IO) {
    val collector = PhoneStatusCollector(context)
    val store = PhoneEventStore(context)
    val nowMs = System.currentTimeMillis()
    val event = collector.buildEvent(nowMs)
    store.append(event, nowMs)

    val batch = store.pendingBatch()
    if (batch.isEmpty()) {
        return@withContext SubmitStatusResult(true, "No pending events")
    }

    val result = BridgeClient().sendEvents(bridgeUrl, token, collector.deviceId(), batch)
    when (result) {
        is SendResult.Success -> {
            store.markAccepted(result.acceptedEventIds)
            SubmitStatusResult(
                true,
                "Sent ${result.acceptedEventIds.size} event(s); pending ${store.count()}",
            )
        }
        SendResult.Unauthorized -> SubmitStatusResult(
            false,
            "Unauthorized. Check the shared token.",
        )
        is SendResult.Failed -> SubmitStatusResult(
            false,
            "Send failed: ${result.message}; pending ${store.count()}",
        )
    }
}

@Preview(showBackground = true)
@Composable
fun SpicaAppPreview() {
    SpicaTheme {
        Text("Spica Companion")
    }
}
