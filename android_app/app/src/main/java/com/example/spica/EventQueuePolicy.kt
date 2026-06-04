package com.example.spica

const val MAX_QUEUE_EVENTS = 200
const val MAX_QUEUE_AGE_MS = 24L * 60L * 60L * 1000L
const val MAX_BATCH_EVENTS = 20

data class QueueEventMeta(
    val eventId: String,
    val occurredAtMs: Long,
)

fun pruneQueue(events: List<QueueEventMeta>, nowMs: Long): List<QueueEventMeta> {
    return events
        .filter { nowMs - it.occurredAtMs <= MAX_QUEUE_AGE_MS }
        .sortedBy { it.occurredAtMs }
        .takeLast(MAX_QUEUE_EVENTS)
}

fun nextBackoffMs(failureCount: Int): Long {
    val steps = longArrayOf(5_000L, 10_000L, 20_000L, 40_000L, 80_000L, 160_000L, 300_000L)
    return steps[failureCount.coerceAtLeast(0).coerceAtMost(steps.lastIndex)]
}
