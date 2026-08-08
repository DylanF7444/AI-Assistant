from assistant.skills.examples import DEFAULT_SKILL_EXAMPLES
from assistant.skills.github import GitHubInput
from assistant.skills.google_drive import GoogleDriveInput
from assistant.skills.notion import NotionInput
from assistant.skills.outlook_mail import OutlookMailInput
from assistant.skills.scheduling import ScheduleManagementInput
from assistant.skills.slack import SlackInput


def test_productivity_write_and_event_examples_match_declared_input_contracts():
    models = {
        "integration.github": GitHubInput,
        "integration.google_drive": GoogleDriveInput,
        "integration.notion": NotionInput,
        "integration.outlook_mail": OutlookMailInput,
        "integration.slack": SlackInput,
        "automation.schedule": ScheduleManagementInput,
    }
    relevant = [example for example in DEFAULT_SKILL_EXAMPLES if example.skill_id in models]

    assert relevant
    for example in relevant:
        if not example.requires_clarification:
            models[example.skill_id].model_validate(example.arguments)
