# Bloub
Source: https://github.com/jeremy-prt/bloub
Commit: b4bb3c1b5f93c7b87a2e8d620f667c4093d97749
License: MIT, copyright 2026 Jérémy Perret. See Bloub-LICENSE.txt.

The unmodified framework-independent TypeScript engine is bundled into
products/app/frontend/shared/bloub-engine.js. The local SVG binding,
bloub-avatar.js, follows the original BloubBot.vue mask, layer and gradient
rendering while integrating Desk lifecycle and accessibility preferences. The Desk binding
mirrors the character, levels eye transforms and reverses gaze yaw so pointer
tracking remains correct after mirroring. Idle states use a local rotating playlist.
The design's upstream x.ai inspiration implies no affiliation.

To reproduce, checkout the commit and create an entry exporting BotEngine
from src/bot/engine, RAYON and DEMI_VIEWBOX from src/bot/repere, STATE_BY_ID
from src/bot/states, and mixHex from src/bot/skins.
Bundle with esbuild 0.28.2, bundle=true, format=esm, target=es2020, without
minification. Preserve the attribution header in the delivered JS.
