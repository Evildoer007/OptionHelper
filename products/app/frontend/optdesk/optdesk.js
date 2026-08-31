import { showWorkspaceStartupFailure, startWorkspace } from "/app/frontend/optchat/optchat.js";

void startWorkspace("desk").catch(showWorkspaceStartupFailure);
