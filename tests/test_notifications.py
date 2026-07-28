from __future__ import annotations

from uuid import uuid4

from assistant.scheduling.notifications import (
    NotificationCenter,
    NotificationMessage,
    NotificationStore,
)
from assistant.storage.database import Database


def test_notification_store_persists_read_and_dismissed_state(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'notifications.db'}")
    database.migrate()
    store = NotificationStore(database)
    run_id = uuid4()
    center = NotificationCenter(store)
    delivered = []
    center.add_listener(delivered.append)

    center.publish(
        NotificationMessage(
            title="Daily report ready",
            message="Open the completed report.",
            severity="success",
            category="report",
            run_id=run_id,
        )
    )

    notification = center.notifications()[0]
    assert delivered == [notification]
    assert center.unread_count() == 1
    assert NotificationCenter(NotificationStore(database)).history() == (notification,)

    read = center.mark_read(notification.notification_id)
    assert read.read_at is not None
    assert center.unread_count() == 0

    dismissed = center.dismiss(notification.notification_id)
    assert dismissed.dismissed_at is not None
    assert center.notifications() == ()
    assert center.notifications(include_dismissed=True) == (dismissed,)


def test_notification_center_redacts_url_credentials_and_query_before_delivery(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'notifications.db'}")
    database.migrate()
    center = NotificationCenter(NotificationStore(database))

    center.publish(
        NotificationMessage(
            title="Download complete",
            message="Saved https://user:secret@example.com/report?token=private",
        )
    )

    assert center.history()[0].message == "Saved https://example.com/report"


def test_notification_store_enforces_history_limit(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'notifications.db'}")
    database.migrate()
    center = NotificationCenter(NotificationStore(database, history_limit=2))

    for index in range(3):
        center.publish(NotificationMessage(title=f"Item {index}", message="Done"))

    assert [item.title for item in center.history()] == ["Item 1", "Item 2"]


def test_in_memory_notification_center_remains_supported():
    center = NotificationCenter(history_limit=2)
    center.publish(NotificationMessage(title="First", message="One"))
    center.publish(NotificationMessage(title="Second", message="Two"))

    first, second = center.history()
    center.mark_read(first.notification_id)
    center.dismiss(second.notification_id)

    assert center.unread_count() == 0
    assert [item.title for item in center.notifications()] == ["First"]
