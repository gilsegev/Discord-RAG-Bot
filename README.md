# TPM Unite RAG Bot

TPM Unite RAG Bot is a community knowledge retrieval project for the TPM Unite Discord server. It helps members find relevant prior discussions, examples, and community-specific guidance from historical TPM Unite conversations.

The project uses retrieval-augmented generation (RAG) so answers are grounded in approved community history instead of generic LLM knowledge. The bot is designed to support discussion, not replace it.

## Why This Exists

TPM Unite previously tested an off-the-shelf Discord AI bot and found that generic responses were not useful for the community. This project takes a different approach: retrieve context from TPM Unite's own public discussion history, then generate answers that reflect the community's actual experience and language.

## Current Status

This repository currently includes:

- Architecture and product planning docs.
- Discord export and data preparation guidance.
- A Python ingestion pipeline for parsing, cleaning, chunking, embedding, and storing Discord history in Qdrant.
- Initial exported chat data for development.

The Discord bot, orchestration workflow, feedback loop, and production observability pieces are still in progress.

## MVP Behavior

For the first version, the bot should:

- Respond only when directly invoked by a user.
- Use retrieved TPM Unite context as the primary source of truth.
- Avoid generic AI advice when relevant community context is unavailable.
- Clearly say when it does not have enough TPM Unite-specific context to answer.
- Encourage members to continue the conversation when a topic is nuanced, personal, subjective, or time-sensitive.

## High-Level Architecture

```text
Discord -> n8n workflow -> Qdrant retrieval -> LLM generation -> Discord response
                         -> Observability and feedback tracking
```

Core components:

- **Discord Gateway:** Receives messages and sends responses.
- **n8n Orchestrator:** Routes requests, assembles context, calls model services, and logs telemetry.
- **Qdrant:** Stores embedded Discord conversation chunks for semantic retrieval.
- **Gemini API:** Generates grounded responses from retrieved context.
- **Observability Layer:** Tracks retrieval quality, latency, failures, and user feedback.

See [docs/Arch overview.md](docs/Arch%20overview.md) for the detailed architecture.

## Repository Structure

```text
docs/        Project architecture and data extraction notes
ingestion/   Discord export parser, chunker, and Qdrant ingestion pipeline
chat_logs/   DiscordChatExporter JSON exports used for development
```

## Local Development

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Run the ingestion pipeline:

```bash
python ingestion/run.py
```

Rebuild the Qdrant collection from scratch:

```bash
python ingestion/run.py --recreate
```

The ingestion pipeline expects DiscordChatExporter JSON files in `chat_logs/` and a local Qdrant instance running on port `6333`.

## Documentation

- [Architecture Overview](docs/Arch%20overview.md)
- [Data Extraction Methodology](docs/Data%20Extraction%20Methodology.md)

## Privacy and Data Boundaries

This project works with community conversation history and should be handled carefully.

- Do not index DMs, private channels, moderator channels, or development channels.
- Do not commit tokens, API keys, credentials, or local environment files.
- Treat exported chat logs as sensitive project data.
- Prefer aggregated metrics over exposing individual user behavior.

## Roadmap

- Finalize retrieval schema and ranking.
- Complete Discord bot invocation flow.
- Wire n8n orchestration for context assembly and response dispatch.
- Add feedback capture on bot responses.
- Add observability for retrieval quality, latency, and context-missing failures.
- Publish weekly bot quality metrics for the community.

## Contributing

Contributions are welcome across ingestion, retrieval, bot integration, prompting, evaluation, observability, privacy, and infrastructure. Please read the docs before making changes that affect data handling, retrieval behavior, or response quality.

## License

No license has been specified yet.
