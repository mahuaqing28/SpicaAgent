package com.example.spica

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class EventQueuePolicyTest {
    @Test
    fun pruneQueueDropsOldEvents() {
        val now = 2_000_000L
        val events = listOf(
            QueueEventMeta("old", now - MAX_QUEUE_AGE_MS - 1),
            QueueEventMeta("fresh", now),
        )

        val pruned = pruneQueue(events, now)

        assertEquals(listOf("fresh"), pruned.map { it.eventId })
    }

    @Test
    fun pruneQueueKeepsLatestTwoHundredEvents() {
        val now = 2_000_000L
        val events = (0..250).map { index ->
            QueueEventMeta("event-$index", now - 250 + index)
        }

        val pruned = pruneQueue(events, now)

        assertEquals(MAX_QUEUE_EVENTS, pruned.size)
        assertEquals("event-51", pruned.first().eventId)
        assertEquals("event-250", pruned.last().eventId)
    }

    @Test
    fun nextBackoffIsCappedAtFiveMinutes() {
        assertEquals(5_000L, nextBackoffMs(0))
        assertEquals(10_000L, nextBackoffMs(1))
        assertEquals(300_000L, nextBackoffMs(99))
    }

    @Test
    fun maxBatchSizeIsTwenty() {
        assertTrue(MAX_BATCH_EVENTS == 20)
    }
}
