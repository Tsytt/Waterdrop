# Waterdrop Codex Skills

A collection of Codex skills for Waterdrop business workflows. Each top-level directory contains one independently installable skill.

## Skills

- `analyzing-sea-ai-badcases`: Analyzes sea.AI robot bad cases by comparing the expected business handling route with the actual trace, validating whether the matched intent or Agent is the correct receiver, mapping evidence to application-, sub-agent-, and expert-level configuration, and producing prioritized recommendations in a fixed report format.
- `booking-waterdrop-rooms`: Schedules a local macOS job that uses the logged-in Feishu user to reserve exactly two non-overlapping 水滴大厦 meeting-room slots at the next day's 09:00 release, with fixed room priority, bounded retries, replacement, cancellation, and local notifications.

## Installation

Use the Skill Installer in Codex to install a specific skill directory from this repository:

```text
Repository: Tsytt/Waterdrop
Path: analyzing-sea-ai-badcases
```

After installation, invoke the skill with:

```text
$analyzing-sea-ai-badcases
```

To install and invoke the Waterdrop meeting-room booking Skill:

```text
Repository: Tsytt/Waterdrop
Path: booking-waterdrop-rooms

$booking-waterdrop-rooms 安排明天抢会议室：10:00-11:00、15:00-16:00
```

Users must have their own access permissions and authenticated sessions for the relevant Feishu resources and the sea.AI platform.
