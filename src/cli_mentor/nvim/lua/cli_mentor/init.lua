-- CLI Mentor session plugin for Neovim.
--
-- Loaded ONLY for a CLI Mentor session, through:
--   nvim --cmd "set rtp^=<this folder>" --cmd "lua require('cli_mentor').start()"
-- Your own Neovim configuration is still loaded and is never modified.
--
-- What it sends to the tutor process (over a private, token-protected Unix
-- socket, using non-blocking libuv writes):
--   * a full snapshot of each observed buffer, then incremental line changes
--     with b:changedtick (unsaved edits included);
--   * save events, mode changes, coalesced cursor position, diagnostics.
-- What it never sends:
--   * buffers outside the approved folders, sensitive files (.env, keys, ...),
--     terminal/help/nofile buffers, unnamed buffers (by default), big files;
--   * raw keystrokes. Optional, per-exercise command COUNTS (like "dw": 2)
--     are recorded only if the learner opts in, only in that exercise's file,
--     only in Normal mode.

local M = {}
local uv = vim.uv or vim.loop

local S = {
  sock = nil,
  token = nil,
  id = nil,
  epoch = nil,
  pipe = nil,
  connected = false,
  connecting = false,
  configured = false,
  paused = true,
  seq = 0,
  backoff = 500,
  roots = {},
  max_bytes = 262144,
  cursor_ms = 250,
  observe_unnamed = false,
  attached = {}, -- bufnr -> { path = string, changes = {}, tick = number, size = number }
  flush_timer = nil,
  cursor_timer = nil,
  telemetry = nil, -- { exercise_id = string, path = string, counts = {}, pending = "" }
  telemetry_ns = nil,
  telemetry_timer = nil,
  reconnect_timer = nil,
  max_write_queue = 4 * 1024 * 1024,
}

local SENSITIVE = {
  "^%.env$", "^%.env%..*$", "^%.envrc$", "%.pem$", "%.key$", "%.p12$", "%.pfx$", "%.kdbx$",
  "%.gpg$", "%.asc$", "^id_[%w_]+$", "^id_[%w_]+%.pub$", "^%.netrc$", "^%.pgpass$", "^%.npmrc$",
  "^%.pypirc$", "^credentials", "^secrets?$", "^secrets?%.", "%.sqlite3?$", "_history$",
}
local DENIED_DIRS = {
  [".git"] = true, [".hg"] = true, [".svn"] = true, ["node_modules"] = true, [".venv"] = true,
  ["venv"] = true, ["__pycache__"] = true, [".mypy_cache"] = true, [".ruff_cache"] = true,
  [".pytest_cache"] = true, [".tox"] = true, ["dist"] = true, ["build"] = true, ["target"] = true,
  [".ssh"] = true, [".gnupg"] = true, [".aws"] = true, [".kube"] = true, [".docker"] = true,
}

-- ------------------------------------------------------------------ helpers
local function now()
  local sec, usec = uv.gettimeofday()
  return sec + usec / 1e6
end

local function realpath(name)
  if name == nil or name == "" then
    return nil
  end
  local full = vim.fn.fnamemodify(name, ":p")
  local real = uv.fs_realpath(full)
  if real then
    return real
  end
  -- New file not written yet: resolve the folder, keep the file name.
  local dir = uv.fs_realpath(vim.fn.fnamemodify(full, ":h"))
  if dir then
    return dir .. "/" .. vim.fn.fnamemodify(full, ":t")
  end
  return full
end

local function within_roots(path)
  for _, root in ipairs(S.roots) do
    if path == root or path:sub(1, #root + 1) == root .. "/" then
      return root
    end
  end
  return nil
end

local function sensitive(path, root)
  local rel = path:sub(#root + 2)
  local parts = vim.split(rel, "/", { plain = true })
  for i = 1, #parts - 1 do
    if DENIED_DIRS[parts[i]] then
      return true
    end
  end
  local base = parts[#parts] or ""
  if DENIED_DIRS[base] then
    return true
  end
  local lower = base:lower()
  for _, pat in ipairs(SENSITIVE) do
    if lower:match(pat) then
      return true
    end
  end
  return false
end

local function buffer_size(buf)
  local n = vim.api.nvim_buf_line_count(buf)
  local ok, size = pcall(vim.api.nvim_buf_get_offset, buf, n)
  return ok and size or 0
end

-- Should this buffer be observed? Returns its real path, or nil.
local function observable(buf)
  if not vim.api.nvim_buf_is_valid(buf) or not vim.api.nvim_buf_is_loaded(buf) then
    return nil
  end
  if vim.bo[buf].buftype ~= "" or vim.bo[buf].binary then
    return nil
  end
  local name = vim.api.nvim_buf_get_name(buf)
  if name == "" then
    return nil -- unnamed buffers are skipped (no path to scope-check)
  end
  local path = realpath(name)
  if not path then
    return nil
  end
  local root = within_roots(path)
  if not root or sensitive(path, root) then
    return nil
  end
  if buffer_size(buf) > S.max_bytes then
    return nil
  end
  return path
end

-- ------------------------------------------------------------------ transport
local function write_line(tbl)
  if not S.pipe or not S.connected then
    return false
  end
  if S.pipe:get_write_queue_size() > S.max_write_queue then
    return false -- the tutor is not keeping up: drop, it will resync
  end
  local ok, data = pcall(vim.json.encode, tbl)
  if not ok then
    return false
  end
  S.pipe:write(data .. "\n")
  return true
end

local function send(payload)
  if not S.connected or not S.configured or S.paused then
    return
  end
  S.seq = S.seq + 1
  payload.seq = S.seq
  payload.ts = now()
  if not write_line(payload) then
    -- Mark every buffer for a fresh snapshot; the engine will also see a gap.
    for _, a in pairs(S.attached) do
      a.needs_snapshot = true
    end
  end
end

-- ------------------------------------------------------------------ buffers
local function snapshot(buf, reason)
  local a = S.attached[buf]
  if not a or not vim.api.nvim_buf_is_valid(buf) then
    return
  end
  a.changes = {}
  a.needs_snapshot = false
  a.tick = vim.api.nvim_buf_get_changedtick(buf)
  send({
    type = "nvim.buffer_snapshot",
    buf = buf,
    path = a.path,
    filetype = vim.bo[buf].filetype or "",
    changedtick = a.tick,
    modified = vim.bo[buf].modified,
    lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false),
    reason = reason,
  })
end

local function flush()
  for buf, a in pairs(S.attached) do
    if a.needs_snapshot then
      snapshot(buf, "resync")
    elseif #a.changes > 0 and vim.api.nvim_buf_is_valid(buf) then
      local changes = a.changes
      a.changes = {}
      send({
        type = "nvim.buffer_lines",
        buf = buf,
        changedtick = a.tick,
        modified = vim.bo[buf].modified,
        changes = changes,
      })
    end
  end
end

local function schedule_flush()
  if not S.flush_timer then
    S.flush_timer = uv.new_timer()
  end
  S.flush_timer:stop()
  S.flush_timer:start(60, 0, vim.schedule_wrap(flush))
end

local function detach(buf, reason)
  if S.attached[buf] then
    S.attached[buf] = nil
    send({ type = "nvim.buffer_detached", buf = buf, reason = reason or "detach" })
  end
end

local function attach(buf)
  if S.attached[buf] then
    return
  end
  local path = observable(buf)
  if not path then
    return
  end
  S.attached[buf] = { path = path, changes = {}, tick = 0, needs_snapshot = false }
  vim.api.nvim_buf_attach(buf, false, {
    on_lines = function(_, b, tick, first, last, new_last)
      local a = S.attached[b]
      if not a then
        return true -- stop listening
      end
      if S.paused or not S.connected then
        a.needs_snapshot = true
        return
      end
      if #a.changes > 400 then
        a.needs_snapshot = true -- too many small changes: one snapshot is cheaper
      end
      if not a.needs_snapshot then
        table.insert(a.changes, {
          first = first,
          last = last,
          new_last = new_last,
          lines = vim.api.nvim_buf_get_lines(b, first, new_last, false),
        })
      end
      a.tick = tick
      schedule_flush()
    end,
    on_reload = function(_, b)
      vim.schedule(function()
        snapshot(b, "reload")
      end)
    end,
    on_detach = function(_, b)
      -- Synchronous on purpose: on :edit! Neovim detaches and then re-reads the
      -- buffer (BufReadPost re-attaches). A delayed "detached" message would
      -- arrive after the new snapshot and wrongly drop the buffer.
      detach(b, "unloaded")
    end,
  })
  snapshot(buf, "attach")
end

local function send_focus(buf)
  local a = S.attached[buf]
  if a then
    send({ type = "nvim.focus", buf = buf, path = a.path })
  end
end

local function send_cursor()
  local buf = vim.api.nvim_get_current_buf()
  if not S.attached[buf] then
    return
  end
  local pos = vim.api.nvim_win_get_cursor(0)
  send({ type = "nvim.cursor", buf = buf, line = pos[1] - 1, col = pos[2] })
end

local function send_diagnostics(buf)
  if not S.attached[buf] then
    return
  end
  local items = {}
  for i, d in ipairs(vim.diagnostic.get(buf)) do
    if i > 100 then
      break
    end
    items[#items + 1] = {
      line = d.lnum,
      col = d.col,
      severity = d.severity or 1,
      message = tostring(d.message or ""):sub(1, 1000),
      source = tostring(d.source or ""):sub(1, 60),
    }
  end
  send({ type = "nvim.diagnostics", buf = buf, items = items })
end

local function resync_all(reason)
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if S.attached[buf] then
      if observable(buf) then
        snapshot(buf, reason)
      else
        detach(buf, "out of scope")
      end
    else
      attach(buf)
    end
  end
  send({ type = "nvim.mode", mode = vim.api.nvim_get_mode().mode, previous = "" })
  send_focus(vim.api.nvim_get_current_buf())
  send_cursor()
end

-- ------------------------------------------------------------------ telemetry (opt-in)
local OPERATORS = { d = true, c = true, y = true }
local MOTIONS = {
  w = true, b = true, e = true, W = true, B = true, E = true, ["0"] = true, ["$"] = true, ["^"] = true,
  G = true, n = true, N = true, ["{"] = true, ["}"] = true, h = true, j = true, k = true, l = true,
}
local CHAR_MOTIONS = { f = true, t = true, F = true, T = true }

local function count(name)
  local t = S.telemetry
  t.counts[name] = (t.counts[name] or 0) + 1
end

local function on_key(key, typed)
  local t = S.telemetry
  if not t or S.paused then
    return
  end
  -- "n" is Normal mode; "no..." is operator-pending (right after d, c, y).
  if vim.api.nvim_get_mode().mode:sub(1, 1) ~= "n" then
    t.pending = ""
    return
  end
  local a = S.attached[vim.api.nvim_get_current_buf()]
  if not a or a.path ~= t.path then
    t.pending = ""
    return
  end
  local k = (typed and typed ~= "") and typed or key
  k = vim.fn.keytrans(k)
  if #k ~= 1 then
    t.pending = "" -- special keys (arrows, <Esc>, ...) reset the parser
    return
  end
  local p = t.pending
  if p:match("^[dcy][ia]$") then -- text object: ciw, daw, ci", ...
    count(p .. k)
    t.pending = ""
  elseif p:match("^[dcy]$") then
    if k == p then
      count(p .. p) -- dd, cc, yy
      t.pending = ""
    elseif k == "i" or k == "a" then
      t.pending = p .. k
    elseif MOTIONS[k] then
      count(p .. k)
      t.pending = ""
    elseif CHAR_MOTIONS[k] then
      t.pending = p .. k
    else
      t.pending = ""
    end
  elseif p:match("^[dcy]?[ftFT]$") then
    count(p) -- the target character itself is never recorded
    t.pending = ""
  elseif p == "g" then
    if k == "g" then
      count("gg")
    end
    t.pending = ""
  elseif k:match("%d") and p == "" then
    -- counts like 5 in 5G are ignored
  elseif OPERATORS[k] then
    t.pending = k
  elseif CHAR_MOTIONS[k] then
    t.pending = k
  elseif k == "g" then
    t.pending = "g"
  elseif MOTIONS[k] or k == "/" or k == "?" then
    count(k)
  end
end

local function flush_telemetry()
  local t = S.telemetry
  if t and next(t.counts) then
    local buf = vim.api.nvim_get_current_buf()
    send({ type = "nvim.technique", exercise_id = t.exercise_id, buf = buf, counts = t.counts })
    t.counts = {}
  end
end

local function set_telemetry(msg)
  if msg.enabled and msg.path and msg.exercise_id then
    S.telemetry = { exercise_id = msg.exercise_id, path = msg.path, counts = {}, pending = "" }
    S.telemetry_ns = vim.on_key(on_key, S.telemetry_ns)
    if not S.telemetry_timer then
      S.telemetry_timer = uv.new_timer()
    end
    S.telemetry_timer:stop()
    S.telemetry_timer:start(1500, 1500, vim.schedule_wrap(flush_telemetry))
  else
    if S.telemetry then
      flush_telemetry()
    end
    S.telemetry = nil
    if S.telemetry_ns then
      vim.on_key(nil, S.telemetry_ns)
    end
    if S.telemetry_timer then
      S.telemetry_timer:stop()
    end
  end
end

-- ------------------------------------------------------------------ control messages
local function on_control(line)
  local ok, msg = pcall(vim.json.decode, line)
  if not ok or type(msg) ~= "table" then
    return
  end
  if msg.ok ~= nil then -- hello reply
    S.configured = true
    S.roots = {}
    for _, r in ipairs(msg.roots or {}) do
      S.roots[#S.roots + 1] = uv.fs_realpath(r) or r
    end
    S.max_bytes = tonumber(msg.max_bytes) or S.max_bytes
    S.cursor_ms = tonumber(msg.cursor_ms) or S.cursor_ms
    S.paused = msg.paused and true or false
    for _, tm in ipairs(msg.telemetry or {}) do
      set_telemetry({ enabled = true, exercise_id = tm.exercise_id, path = tm.path })
    end
    if not S.paused then
      resync_all("attach")
    end
  elseif msg.cmd == "pause" then
    flush()
    S.paused = true
  elseif msg.cmd == "resume" then
    S.paused = false
    resync_all("resume")
  elseif msg.cmd == "resync" then
    resync_all("resync")
  elseif msg.cmd == "telemetry" then
    set_telemetry(msg)
  end
end

-- ------------------------------------------------------------------ connection
local connect

local function schedule_reconnect()
  if not S.reconnect_timer then
    S.reconnect_timer = uv.new_timer()
  end
  S.reconnect_timer:stop()
  S.reconnect_timer:start(S.backoff, 0, vim.schedule_wrap(function()
    connect()
  end))
  S.backoff = math.min(S.backoff * 2, 30000)
end

local function on_disconnect()
  S.connected = false
  S.configured = false
  if S.pipe then
    pcall(function()
      S.pipe:close()
    end)
    S.pipe = nil
  end
  for _, a in pairs(S.attached) do
    a.needs_snapshot = true
  end
  schedule_reconnect()
end

connect = function()
  if S.connecting or S.connected or not S.sock then
    return
  end
  S.connecting = true
  local pipe = uv.new_pipe(false)
  pipe:connect(S.sock, function(err)
    S.connecting = false
    if err then
      pcall(function()
        pipe:close()
      end)
      vim.schedule(schedule_reconnect)
      return
    end
    S.pipe = pipe
    S.connected = true
    S.backoff = 500
    write_line({ v = 1, token = S.token, kind = "nvim", id = S.id, epoch = S.epoch })
    local buffer = ""
    pipe:read_start(function(rerr, chunk)
      if rerr or not chunk then
        vim.schedule(on_disconnect)
        return
      end
      buffer = buffer .. chunk
      while true do
        local nl = buffer:find("\n", 1, true)
        if not nl then
          break
        end
        local line = buffer:sub(1, nl - 1)
        buffer = buffer:sub(nl + 1)
        vim.schedule(function()
          on_control(line)
        end)
      end
    end)
  end)
end

-- ------------------------------------------------------------------ autocommands
local function setup_autocmds()
  local group = vim.api.nvim_create_augroup("CliMentor", { clear = true })
  vim.api.nvim_create_autocmd({ "BufReadPost", "BufNewFile", "BufEnter", "BufWinEnter" }, {
    group = group,
    callback = function(args)
      if S.configured and not S.paused then
        attach(args.buf)
        if args.event == "BufEnter" then
          send_focus(args.buf)
        end
      end
    end,
  })
  vim.api.nvim_create_autocmd("BufFilePost", {
    group = group,
    callback = function(args)
      if S.attached[args.buf] then
        local path = observable(args.buf)
        if path then
          S.attached[args.buf].path = path
          snapshot(args.buf, "attach")
        else
          detach(args.buf, "renamed out of scope")
        end
      else
        attach(args.buf)
      end
    end,
  })
  vim.api.nvim_create_autocmd("BufWritePost", {
    group = group,
    callback = function(args)
      if not S.attached[args.buf] then
        attach(args.buf)
      end
      local a = S.attached[args.buf]
      if a then
        flush()
        send({
          type = "nvim.buffer_written",
          buf = args.buf,
          path = a.path,
          changedtick = vim.api.nvim_buf_get_changedtick(args.buf),
        })
      end
    end,
  })
  vim.api.nvim_create_autocmd({ "BufDelete", "BufWipeout" }, {
    group = group,
    callback = function(args)
      detach(args.buf, "closed")
    end,
  })
  vim.api.nvim_create_autocmd("ModeChanged", {
    group = group,
    callback = function()
      local ev = vim.v.event or {}
      send({ type = "nvim.mode", mode = ev.new_mode or vim.api.nvim_get_mode().mode, previous = ev.old_mode or "" })
    end,
  })
  vim.api.nvim_create_autocmd({ "CursorMoved", "CursorMovedI" }, {
    group = group,
    callback = function()
      if not S.cursor_timer then
        S.cursor_timer = uv.new_timer()
      end
      if not S.cursor_timer:is_active() then
        S.cursor_timer:start(S.cursor_ms, 0, vim.schedule_wrap(send_cursor))
      end
    end,
  })
  vim.api.nvim_create_autocmd("DiagnosticChanged", {
    group = group,
    callback = function(args)
      send_diagnostics(args.buf)
    end,
  })
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function()
      flush()
      flush_telemetry()
      if S.pipe then
        pcall(function()
          S.pipe:close()
        end)
      end
    end,
  })
end

-- ------------------------------------------------------------------ public
function M.start()
  local sock = vim.env.CLI_MENTOR_SOCK
  local token_file = vim.env.CLI_MENTOR_TOKEN_FILE
  -- Do not pass these on to :terminal or :! programs.
  vim.env.CLI_MENTOR_SOCK = nil
  vim.env.CLI_MENTOR_TOKEN_FILE = nil
  if not sock or sock == "" or not token_file or token_file == "" then
    return
  end
  local fh = io.open(token_file, "r")
  if not fh then
    return
  end
  S.token = (fh:read("*a") or ""):gsub("%s+", "")
  fh:close()
  S.sock = sock
  S.id = "nvim-" .. tostring(vim.fn.getpid())
  S.epoch = tostring(os.time()) .. "-" .. tostring(vim.fn.getpid())
  setup_autocmds()
  connect()
end

-- For :checkhealth-style inspection and tests. Contains no secrets.
function M.status()
  local bufs = {}
  for buf, a in pairs(S.attached) do
    bufs[#bufs + 1] = { buf = buf, path = a.path }
  end
  return {
    connected = S.connected,
    configured = S.configured,
    paused = S.paused,
    seq = S.seq,
    attached = bufs,
    telemetry = S.telemetry and S.telemetry.exercise_id or nil,
  }
end

return M
