# Waterdrop Codex Skills

A collection of Codex skills for Waterdrop business workflows. Each top-level directory contains one independently installable skill.

## Skills

- `analyzing-sea-ai-badcases`: Analyzes sea.AI robot bad cases by comparing the expected business handling route with the actual trace, validating whether the matched intent or Agent is the correct receiver, mapping evidence to application-, sub-agent-, and expert-level configuration, and producing prioritized recommendations in a fixed report format.

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

Users must have their own access permissions and authenticated sessions for the required Feishu documents and the sea.AI platform.
