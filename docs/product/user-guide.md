# PyCat user guide

[Back to the introduction](../../README_en.md) · [简体中文](user-guide_zh.md) · [Releases](https://github.com/nanhuayu/pycat/releases)

This guide starts with desktop use. Features can differ between versions; also read the notes for the version you install. English setting names below describe the corresponding controls shown in the screenshots.

## 1. Install and connect a model

Choose an available package for your system from Releases. Extract the complete Windows portable archive, keep the `pycat-windows-x64` folder together, and open **`pycat.exe`**. Do not copy only the executable. `pycat-cli.exe` is the command-line entrypoint and is not needed for normal desktop use.

In **Settings → Models & services**, configure a service you can access and select a default model. A local model service must already be installed and running. PyCat does not include model credits; availability and costs depend on your provider or local setup.

Choose a **Login method** on the service's connection page:

- **ChatGPT / Codex:** sign in, complete ChatGPT authorization in the browser, then return to PyCat and select an available model.
- **WorkBuddy / CodeBuddy (mainland China, experimental):** authorize with a mainland China account in the browser, then select an available model. This integration uses the CodeBuddy CLI client identity.
- **API key:** enter the service address, API type and the provider's key. Follow the authentication requirements of any local service.

Use “Sync models” to inspect the account's model catalog, and “Sign out” to disconnect the account. Account plans and API-key quotas are managed separately by the provider.

Try a short question before processing larger files. Account-login integrations are experimental and may change with upstream services. If one fails, use a supported standard connection offered by your provider.

## 2. Choose your materials and scope

Create or select a project folder in the sidebar, then start a conversation. Add files with the attachment button or paste a screenshot. Before organizing or modifying files, check the project shown at the top.

The permission menu beside the composer controls approvals and file scope. Current desktop defaults allow **automatic execution and access to all local paths**. You can request confirmation or restrict file access to the project. File scope is not a system sandbox for every external program; shells and third-party tools have their own behavior.

Use Chat for ordinary questions. Choose an appropriate Agent mode when a task requires tools, file changes or several steps.

## 3. Start small

Put a few public documents in a new folder and try:

> Read the materials in this project. List five main points and mark uncertainties. Save a short brief without modifying the original files.

Give a clear goal, allowed scope and output format. Continue in the same conversation with requests such as “add sources,” “shorten this to one page,” or “make a checklist from the result.”

Inspect progress and tool steps while it works. Stopping a run does not undo file changes or external actions that have already happened. Keep your own backups of important files and review results.

![Desktop layout: projects, conversation, inline results and the task composer](../../media/screenshots/workspace.png)

*This actual interface uses isolated demonstration materials to explain the layout. See the [project introduction](../../README_en.md) for user-supplied task screenshots.*

## 4. Review and reuse results

Generated images appear directly in the conversation. Click an image or file to preview it. The image context menu offers copying, saving and continued editing; add your changes before sending the next request.

Images, SVG, Markdown, text and PDF have previews. Some Office documents display extracted content rather than the exact layout of the original office application. Use **Materials & memory** to find results, organize project information and manage reusable memories and preferences.

![The actual PyCat image preview with a sample SVG](../../media/screenshots/svg-preview.png)

*This screenshot uses isolated demonstration materials, without personal conversations or real credentials.*

## 5. Generate images or recognize text

**Images:** configure an image-capable service and model in Models & services, then select it for image generation and editing in **Tools & capabilities → Capabilities**. It can differ from your chat model. Attach a reference image or use “Continue editing image” on an existing result.

**Text recognition:** choose local recognition or a vision model in **Tools & capabilities → OCR**. Vision models need a working model connection; you can adjust the recognition prompt. Local OCR needs the relevant components in your installation.

Review important names, numbers and conclusions. Scans, charts and small text vary in quality. Image input support does not imply image generation support.

## 6. Add skills and tools as needed

In **Settings → Tools & capabilities**:

- **Skills** provide reusable working methods. Manage installed skills or look for suitable sources in the discovery page.
- **MCP** connects external tools and services. A tool may still need service configuration, credentials or local dependencies.
- **Computer & browser** requires an available backend and may need additional installation and system permissions.

Review purpose and source before installing. Update controls check supported extensions; externally managed installations may need their original update method. Installing a skill does not configure every model, tool or permission it may need.

## 7. Connect a messaging platform

Add a platform in **Settings → Messaging channels**, supply its bot credentials and bind a project conversation. Feishu / Lark, DingTalk, Telegram, QQ and WeChat have different setup requirements.

Keep the PyCat computer running and connected. Verify text, image and file exchange in a test chat before using it for real work. Supported message types depend on the connection; the WeChat official-account connection currently focuses on text.

Shared attachment limits are 25 MiB per file, eight files per batch and 64 MiB total; a platform may impose lower limits. Audio and video attachments are not automatically transcribed. Messaging replies send explicitly delivered results from the current turn, not the entire project folder.

## Troubleshooting

| Situation | First check |
| --- | --- |
| A model does not reply | Service availability, credentials, model access and the displayed error. |
| A file or result cannot be opened | The selected project and conversation, file existence, changes to the file and permission scope. |
| Image generation fails | The image model, credits, requested dimensions and reference-image requirements. |
| Work appears stuck | Whether it is awaiting your answer, waiting for a program or reporting a failure. |
| A bot does not reply | PyCat is online, the connection is active, platform permissions are enabled and the conversation is bound. |

Projects and conversations are stored locally. Cloud models, online tools and messaging services receive the content sent to them; choose services and permissions appropriate for your materials.

For help, open an [issue](https://github.com/nanhuayu/pycat/issues) with the version, system, reproduction steps and a redacted screenshot. Do not include API keys, bot secrets or private files.
