package com.example.spica

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

class PhoneEventStore(context: Context) {
    private val preferences = context.getSharedPreferences("phone_events", Context.MODE_PRIVATE)

    @Synchronized
    fun append(event: JSONObject, nowMs: Long) {
        val events = readEvents().toMutableList()
        events.add(event)
        writeEvents(pruneJsonEvents(events, nowMs))
    }

    @Synchronized
    fun pendingBatch(limit: Int = MAX_BATCH_EVENTS): List<JSONObject> {
        return readEvents()
            .sortedBy { it.optLong("occurred_at_ms") }
            .take(limit)
    }

    @Synchronized
    fun markAccepted(acceptedEventIds: Set<String>) {
        if (acceptedEventIds.isEmpty()) return
        writeEvents(readEvents().filterNot { acceptedEventIds.contains(it.optString("event_id")) })
    }

    @Synchronized
    fun count(): Int = readEvents().size

    @Synchronized
    fun prune(nowMs: Long) {
        writeEvents(pruneJsonEvents(readEvents(), nowMs))
    }

    private fun readEvents(): List<JSONObject> {
        val raw = preferences.getString(KEY_EVENTS, "[]") ?: "[]"
        val array = runCatching { JSONArray(raw) }.getOrElse { JSONArray() }
        return (0 until array.length())
            .mapNotNull { index -> array.optJSONObject(index) }
    }

    private fun writeEvents(events: List<JSONObject>) {
        val array = JSONArray()
        events.forEach { array.put(it) }
        preferences.edit().putString(KEY_EVENTS, array.toString()).apply()
    }

    private fun pruneJsonEvents(events: List<JSONObject>, nowMs: Long): List<JSONObject> {
        val metas = events.map {
            QueueEventMeta(
                eventId = it.optString("event_id"),
                occurredAtMs = it.optLong("occurred_at_ms"),
            )
        }
        val keptIds = pruneQueue(metas, nowMs).map { it.eventId }.toSet()
        return events.filter { keptIds.contains(it.optString("event_id")) }
    }

    private companion object {
        const val KEY_EVENTS = "events"
    }
}
