package com.example.spica

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

sealed class SendResult {
    data class Success(val acceptedEventIds: Set<String>) : SendResult()
    data object Unauthorized : SendResult()
    data class Failed(val message: String) : SendResult()
}

class BridgeClient {
    fun sendEvents(baseUrl: String, token: String, deviceId: String, events: List<JSONObject>): SendResult {
        if (baseUrl.isBlank()) return SendResult.Failed("Bridge URL is empty")
        if (token.isBlank()) return SendResult.Unauthorized

        val endpoint = baseUrl.trimEnd('/') + "/api/phone/events"
        val payload = JSONObject()
            .put("device_id", deviceId)
            .put("events", JSONArray().also { array ->
                events.forEach { array.put(it) }
            })
            .toString()

        return try {
            val connection = (URL(endpoint).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = 5000
                readTimeout = 10000
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("Authorization", "Bearer $token")
            }
            connection.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }
            val status = connection.responseCode
            val raw = if (status in 200..299) {
                connection.inputStream.bufferedReader().use { it.readText() }
            } else {
                connection.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
            }
            connection.disconnect()

            when (status) {
                200 -> {
                    val json = JSONObject(raw)
                    val accepted = json.optJSONArray("accepted_event_ids") ?: JSONArray()
                    SendResult.Success((0 until accepted.length()).map { accepted.getString(it) }.toSet())
                }
                401 -> SendResult.Unauthorized
                else -> SendResult.Failed("HTTP $status")
            }
        } catch (exception: IOException) {
            SendResult.Failed(exception.message ?: "Network error")
        } catch (exception: Exception) {
            SendResult.Failed(exception.message ?: "Send failed")
        }
    }
}
