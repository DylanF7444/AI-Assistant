# Personal Device Assistant Feature Checklist

Status updated July 5, 2026. Implemented built-in skills are listed in the
normalized catalog at `docs/FEATURES.md`.

## Core Assistant Behavior

- [X] Accept natural language commands
- [ ] Answer general questions
- [X] Maintain conversational context
- [X] Ask clarifying questions when needed
- [ ] Summarize long text
- [ ] Rewrite or improve text
- [ ] Translate text
- [ ] Explain concepts simply
- [ ] Provide step-by-step instructions
- [ ] Remember user preferences with permission
- [X] Forget saved information on request
- [X] Work offline for basic local tasks where possible
- [X] Clearly explain what it can and cannot do

## Voice Interaction

- [X] Wake word or push-to-talk activation
- [X] Speech-to-text input
- [X] Text-to-speech output
- [X] Interruptible speech playback
- [X] Adjustable voice speed
- [X] Adjustable voice volume
- [X] Multiple voice options
- [X] Hands-free mode
- [X] Confirmation before sensitive actions

## Device Control

- [X] Open applications
- [X] Close applications
- [X] Switch between open apps (focus, minimize)
- [X] Search installed apps
- [X] Adjust system volume
- [X] Mute/unmute audio
- [X] Adjust screen brightness
- [X] Toggle Wi-Fi
- [X] Toggle Bluetooth
- [X] Toggle dark/light mode
- [X] Lock the device
- [X] Restart the device with confirmation
- [X] Shut down the device with confirmation
- [X] Show battery status
- [X] Show storage status
- [X] Show memory/CPU usage
- [X] Manage connected devices

## File Management

- [X] Search files by name
- [ ] Search files by content
- [X] Open files
- [X] Move files
- [X] Copy files
- [X] Rename files
- [X] Delete files with confirmation
- [ ] Restore files from trash/recycle bin
- [ ] Create folders
- [ ] Organize files by type/date/project
- [ ] Detect duplicate files
- [ ] Compress files into archives
- [ ] Extract archives
- [ ] Convert file formats
- [ ] Summarize documents
- [ ] Compare two files
- [ ] Watch folders for changes

## Notes and Writing

- [X] Create notes
- [X] Search notes
- [ ] Edit notes
- [ ] Categorize notes
- [ ] Tag notes
- [ ] Summarize notes
- [ ] Turn notes into tasks
- [ ] Draft emails
- [ ] Draft messages
- [ ] Draft documents
- [ ] Improve grammar
- [ ] Change writing tone
- [ ] Generate outlines
- [ ] Create checklists
- [ ] Create meeting notes
- [ ] Extract action items

## Calendar and Scheduling

- [ ] View today’s calendar
- [ ] View upcoming events
- [ ] Create calendar events
- [ ] Edit calendar events
- [ ] Delete calendar events with confirmation
- [ ] Detect scheduling conflicts
- [ ] Suggest meeting times
- [ ] Set event reminders
- [ ] Join online meetings
- [ ] Open meeting links
- [ ] Summarize daily schedule
- [ ] Prepare agenda from notes
- [ ] Remind user before events

## Tasks and Reminders

- [X] Create tasks
- [ ] Edit tasks
- [X] Complete tasks
- [X] Delete tasks
- [ ] Set due dates
- [ ] Set priorities
- [ ] Create recurring reminders
- [ ] Create location-based reminders if supported
- [X] Create time-based reminders
- [ ] Group tasks by project
- [ ] Show overdue tasks
- [ ] Show today’s tasks
- [ ] Break large tasks into steps
- [ ] Suggest next actions

## Email

- [ ] Read inbox summaries
- [ ] Search emails
- [ ] Draft replies
- [ ] Send emails with confirmation
- [ ] Archive emails
- [ ] Delete emails with confirmation
- [ ] Mark emails as read/unread
- [ ] Flag important emails
- [ ] Extract action items from emails
- [ ] Summarize email threads
- [ ] Identify urgent emails
- [ ] Detect possible phishing attempts
- [ ] Add email attachments
- [ ] Save email attachments

## Messaging

- [ ] Read recent messages
- [ ] Send messages with confirmation
- [ ] Draft message replies
- [ ] Search message history
- [ ] Summarize conversations
- [ ] Extract tasks from messages
- [ ] Identify unanswered messages
- [ ] Respect do-not-disturb mode

## Web and Search

- [X] Search the web
- [X] Open websites
- [ ] Summarize webpages
- [ ] Extract key facts from webpages
- [ ] Compare search results
- [ ] Track sources for answers
- [ ] Open specific browser tabs
- [ ] Search browser history with permission
- [ ] Manage bookmarks
- [ ] Fill simple web forms with confirmation
- [ ] Monitor webpages for changes
- [ ] Save webpages for later

## Local App Automation

- [X] Control browser
- [ ] Control file explorer
- [ ] Control terminal
- [ ] Control code editor
- [X] Control media player
- [ ] Control office apps
- [ ] Control notes apps
- [ ] Control calendar apps
- [X] Use keyboard shortcuts
- [X] Click UI elements
- [X] Type into focused fields
- [ ] Read visible screen content
- [X] Perform multi-step workflows
- [ ] Recover from failed UI actions

## Coding and Development

- [ ] Open projects
- [ ] Search code
- [ ] Explain code
- [ ] Edit code
- [ ] Generate code
- [ ] Refactor code
- [ ] Run tests
- [ ] Debug errors
- [ ] Read terminal output
- [ ] Install dependencies with confirmation
- [ ] Start dev servers
- [ ] Inspect logs
- [ ] Create commits
- [ ] Write commit messages
- [ ] Review diffs
- [ ] Generate documentation
- [ ] Create scripts
- [ ] Explain stack traces
- [ ] Work with Git branches
- [ ] Open pull requests if connected

## Documents

- [ ] Read PDF files
- [ ] Summarize PDF files
- [ ] Extract text from PDFs
- [ ] Extract tables from PDFs
- [ ] Create Word documents
- [ ] Edit Word documents
- [ ] Create PDFs
- [ ] Convert documents to PDF
- [ ] Compare documents
- [ ] Redline or suggest edits
- [ ] Format documents
- [ ] Generate reports
- [ ] Create templates
- [ ] Fill forms with confirmation

## Spreadsheets

- [ ] Open spreadsheets
- [ ] Read spreadsheet data
- [ ] Summarize spreadsheet data
- [ ] Clean spreadsheet data
- [ ] Create formulas
- [ ] Explain formulas
- [ ] Generate charts
- [ ] Sort data
- [ ] Filter data
- [ ] Detect duplicates
- [ ] Validate data
- [ ] Create pivot tables
- [ ] Export CSV files
- [ ] Import CSV files
- [ ] Generate financial summaries
- [ ] Track budgets

## Presentations

- [ ] Create slide decks
- [ ] Edit slide decks
- [ ] Summarize slide decks
- [ ] Generate slide outlines
- [ ] Improve slide copy
- [ ] Add speaker notes
- [ ] Create charts for slides
- [ ] Export presentations to PDF
- [ ] Apply consistent formatting

## Media

- [ ] Play music
- [X] Pause/resume media
- [X] Skip tracks
- [X] Adjust media volume
- [ ] Search local media files
- [ ] Open photos
- [ ] Organize photos
- [ ] Rename media files
- [ ] Convert audio/video formats
- [ ] Trim audio/video clips
- [ ] Generate image descriptions
- [ ] Extract text from images
- [ ] Create images from prompts
- [ ] Edit images from instructions

## Screen Understanding

- [ ] Read text from the screen
- [ ] Describe visible windows
- [ ] Identify active application
- [ ] Identify buttons and fields
- [ ] Take screenshots with permission
- [ ] Explain error dialogs
- [ ] Guide the user through visible UI
- [ ] Detect when a workflow is complete
- [ ] Avoid capturing sensitive content unless allowed

## Personal Knowledge Base

- [X] Save useful facts
- [X] Search saved knowledge
- [ ] Link notes, files, and tasks
- [ ] Summarize personal projects
- [ ] Track project status
- [ ] Build timelines from notes
- [ ] Answer questions from local documents
- [ ] Cite source files when answering
- [ ] Detect outdated saved information
- [ ] Let the user review saved memory

## Automation

- [X] Create one-time automations
- [ ] Create recurring automations
- [X] Run automations manually
- [ ] Pause automations
- [ ] Delete automations
- [ ] Trigger automations by time
- [ ] Trigger automations by file changes
- [ ] Trigger automations by calendar events
- [ ] Trigger automations by app state
- [ ] Send scheduled summaries
- [ ] Monitor folders
- [ ] Monitor websites
- [ ] Notify user when conditions are met
- [X] Require confirmation for risky automation actions

## Notifications

- [X] Show desktop notifications
- [ ] Read notifications aloud
- [ ] Summarize missed notifications
- [ ] Prioritize important notifications
- [ ] Silence low-priority notifications
- [ ] Respect focus/do-not-disturb settings
- [ ] Notify about reminders
- [ ] Notify about completed automations
- [ ] Notify about failed actions

## Privacy and Security

- [X] Ask permission before accessing sensitive data
- [ ] Ask confirmation before sending messages/emails
- [X] Ask confirmation before deleting files
- [ ] Ask confirmation before purchases/payments
- [ ] Ask confirmation before installing software
- [X] Redact sensitive data in logs
- [X] Store credentials securely
- [X] Support local-only mode
- [X] Show what data was accessed
- [X] Allow user to revoke permissions
- [X] Allow user to delete stored memory
- [X] Avoid reading private content unnecessarily
- [ ] Detect suspicious links/files
- [ ] Warn before running unknown scripts

## Accounts and Integrations

- [ ] Connect email accounts
- [ ] Connect calendar accounts
- [ ] Connect cloud storage
- [ ] Connect notes apps
- [ ] Connect task apps
- [ ] Connect messaging apps
- [X] Connect smart home devices
- [ ] Connect music services
- [ ] Connect browser profiles
- [ ] Connect developer tools
- [X] Manage integration permissions
- [ ] Handle expired logins gracefully

## Smart Home and External Devices

- [X] Control lights
- [X] Control thermostat
- [X] Control plugs
- [X] Control speakers
- [X] Control displays
- [X] Check device status
- [X] Run smart home scenes
- [ ] Schedule smart home actions
- [ ] Confirm security-sensitive actions
- [ ] Notify when devices go offline

## Health, Focus, and Productivity

- [ ] Start focus sessions
- [ ] Block distracting apps/sites
- [ ] Track work sessions
- [ ] Suggest breaks
- [X] Set timers
- [X] Set alarms
- [ ] Track habits
- [ ] Generate daily plans
- [ ] Generate weekly reviews
- [ ] Summarize completed work
- [ ] Identify unfinished work
- [ ] Suggest task priorities

## Finance and Shopping

- [ ] Track expenses from files/emails
- [ ] Summarize spending
- [ ] Manage budgets
- [ ] Compare prices
- [ ] Track packages
- [ ] Create shopping lists
- [ ] Search for products
- [ ] Warn about subscriptions
- [ ] Detect duplicate subscriptions
- [ ] Require confirmation before purchases
- [ ] Never expose financial credentials

## Travel and Location

- [ ] Check weather
- [ ] Get directions
- [ ] Estimate travel time
- [ ] Track flights
- [ ] Track hotel reservations
- [ ] Summarize travel plans
- [ ] Create packing lists
- [ ] Add travel events to calendar
- [ ] Notify about delays
- [ ] Work with location permissions

## Accessibility

- [ ] Read screen content aloud
- [ ] Dictation support
- [ ] Voice navigation
- [ ] High contrast mode support
- [ ] Large text support
- [ ] Keyboard-only operation
- [ ] Captions for audio output
- [ ] Simple language mode
- [ ] Reduce motion option
- [ ] Confirm actions verbally
- [ ] Provide error recovery guidance

## Reliability

- [X] Handle failed commands gracefully
- [X] Retry temporary failures
- [X] Explain why an action failed
- [X] Keep logs of actions
- [X] Let user inspect recent actions
- [X] Undo reversible actions
- [X] Avoid duplicate actions
- [X] Detect missing permissions
- [X] Detect offline state
- [X] Continue long-running tasks in background
- [X] Notify when long-running tasks finish

## User Experience

- [ ] Quick command palette
- [X] Chat interface
- [X] Voice interface
- [X] System tray/menu bar access
- [X] Keyboard shortcut activation
- [X] Clear action confirmations
- [X] Clear progress indicators
- [X] Clear error messages
- [ ] Customizable assistant name
- [ ] Customizable personality/tone
- [X] Customizable permissions
- [X] Customizable notification behavior
- [ ] Onboarding flow
- [X] Settings screen
- [X] Activity history
- [X] Help screen

## Advanced Reasoning

- [X] Break complex goals into steps
- [X] Track progress across steps
- [X] Use tools when needed
- [X] Decide when to ask for confirmation
- [ ] Compare multiple options
- [ ] Explain tradeoffs
- [X] Create plans
- [ ] Revise plans as conditions change
- [ ] Detect contradictions in user requests
- [X] Identify missing information
- [X] Maintain long-running project context

## Safety Boundaries

- [ ] Refuse harmful requests
- [ ] Refuse credential theft
- [ ] Refuse malware creation
- [ ] Refuse unauthorized access
- [ ] Refuse surveillance without consent
- [X] Warn about risky commands
- [X] Require explicit approval for irreversible actions
- [X] Separate user-approved actions from autonomous actions
- [X] Log sensitive actions
- [ ] Provide safe alternatives when refusing

## Testing and Verification

- [X] Unit tests for core assistant logic
- [X] Integration tests for tools
- [ ] UI tests for main workflows
- [ ] Voice input tests
- [ ] Voice output tests
- [X] Permission tests
- [X] File operation tests
- [X] Automation tests
- [X] Failure recovery tests
- [X] Offline mode tests
- [X] Privacy/security tests
- [X] Performance tests
- [ ] Cross-platform tests if supported
- [ ] Manual end-to-end test checklist
