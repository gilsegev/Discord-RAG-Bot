# How We Work: Community Knowledge RAG Bot

This document outlines the operational cadence, communication channels, and engineering expectations for our project. Please review these guidelines to ensure we are aligned as we build the architecture.

## 1. Communication

All asynchronous communication will take place in the dedicated bot development channel on Discord. If conversations become too dense or complex, we will transition to a forum format to enable focused sub-channels.

## 2. Discord Notifications

I will create a specific role and notification group for all project contributors. This will override the default server settings which supresses notifications for larger commuties. If you prefer to opt out of these alerts, let me know directly.

## 3. Version Control and Code Quality

We use Git for version control. The repository is located here: https://github.com/gilsegev/Discord-RAG-Bot

All work must be submitted via Pull Requests. Do not push directly to the main branch.

- Test your code and workflows locally before submitting a PR.
- Include a brief description of what changed and how it was tested in each PR.
- I will review and merge PRs into the main branch to maintain the integrity of the production environment.

## 4. Responsibilities and Documentation

Specific responsibilities and tasks are defined in the Vision and Requirements document: https://docs.google.com/document/d/1rscJ2LxxrtGDwG_Hx7VLDQ3zSS5k0Ih7OqRBG8I-Ngs/edit?tab=t.0#heading=h.rbarf18omsbe

Before building a new component, create a brief one-page design document outlining your approach. Upload this document to the `docs` folder in our GitHub repository so the team can review the architecture before code is written.

## 5. Blockers and Support

If you get stuck or find that you do not have the capacity to complete a task, bring it up with the team in the channel or message me directly as soon as possible. We will adjust workloads or provide technical support to keep the project moving.

## 6. Development Environments

Contributors will build and test their components locally. You will use Docker to spin up local instances of n8n and Qdrant. Once your n8n workflow or ingestion script is complete and tested, export the files and push them to GitHub via a PR. I will manage the deployment of approved code to the production Oracle server.

## 7. Sync Meeting

We will hold a synchronization meeting next week on Friday  6/5 at 10:00 AM PST. All contributors are encouraged to attend. We will review the architecture, assign the first batch of tasks, and clear up any immediate questions.
