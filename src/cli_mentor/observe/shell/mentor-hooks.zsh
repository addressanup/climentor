# CLI Mentor session hooks for zsh.
#
# Loaded ONLY inside a CLI Mentor shell pane (through a private ZDOTDIR).
# Your own ~/.zshrc is loaded first and is never edited.
#
# What it records, per command:  command text, start/end time, exit status,
# working directory, tmux pane id.
# What it never does:            run any observed text, read your keystrokes,
#                                record commands that start with a space.
#
# Each event opens a short Unix-socket connection to the tutor process,
# authenticates with the session token, sends one line, and closes.

zmodload zsh/net/socket 2>/dev/null || return 0
zmodload zsh/datetime 2>/dev/null || return 0

typeset -g _mentor_sock="${CLI_MENTOR_SOCK:-}"
typeset -g _mentor_token=""
if [[ -n ${CLI_MENTOR_TOKEN_FILE:-} && -r ${CLI_MENTOR_TOKEN_FILE} ]]; then
  _mentor_token="$(<"${CLI_MENTOR_TOKEN_FILE}")"
fi
typeset -g _mentor_nvim_rtp="${CLI_MENTOR_NVIM_RTP:-}"
typeset -g _mentor_token_file="${CLI_MENTOR_TOKEN_FILE:-}"
typeset -g _mentor_id="zsh-$$"
typeset -g _mentor_epoch="${EPOCHSECONDS}-$$"
typeset -gi _mentor_seq=0
typeset -g _mentor_start=""
typeset -g _mentor_cmd=""

# Keep these out of the environment of programs you run.
unset CLI_MENTOR_TOKEN_FILE CLI_MENTOR_SOCK CLI_MENTOR_NVIM_RTP

[[ -n $_mentor_sock && -n $_mentor_token ]] || return 0

# Escape a string for JSON. Result in $REPLY.
_mentor_json() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  s=${s//[[:cntrl:]]/}
  REPLY=$s
}

_mentor_now() {
  REPLY=${EPOCHREALTIME/,/.}
}

# Send one event. $1 is the JSON body (without the braces) of the payload.
_mentor_send() {
  (( _mentor_seq++ ))
  [[ -S $_mentor_sock ]] || return 0
  zsocket "$_mentor_sock" 2>/dev/null || return 0
  local fd=$REPLY
  local ts; _mentor_now; ts=$REPLY
  print -r -u $fd -- "{\"v\":1,\"token\":\"$_mentor_token\",\"kind\":\"shell\",\"id\":\"$_mentor_id\",\"epoch\":\"$_mentor_epoch\"}" 2>/dev/null
  print -r -u $fd -- "{\"seq\":$_mentor_seq,\"ts\":$ts,$1}" 2>/dev/null
  exec {fd}>&-
  return 0
}

_mentor_preexec() {
  _mentor_start=""
  _mentor_cmd=""
  # Convention: a leading space means "do not record this command".
  [[ $1 == ' '* ]] && return 0
  _mentor_now; _mentor_start=$REPLY
  _mentor_cmd=$1
  local c d
  _mentor_json "$1"; c=$REPLY
  _mentor_json "$PWD"; d=$REPLY
  _mentor_send "\"type\":\"shell.command_started\",\"command\":\"$c\",\"cwd\":\"$d\",\"pane\":\"${TMUX_PANE:-}\",\"started_at\":$_mentor_start"
}

_mentor_precmd() {
  local st=$?
  if [[ -n $_mentor_start ]]; then
    local c d now
    _mentor_now; now=$REPLY
    _mentor_json "$_mentor_cmd"; c=$REPLY
    _mentor_json "$PWD"; d=$REPLY
    _mentor_send "\"type\":\"shell.command_finished\",\"command\":\"$c\",\"cwd\":\"$d\",\"exit_status\":$st,\"pane\":\"${TMUX_PANE:-}\",\"started_at\":$_mentor_start,\"finished_at\":$now"
    _mentor_start=""
    _mentor_cmd=""
  fi
  return $st
}

# Run first, so we see the real exit status and pass it on unchanged.
typeset -ga preexec_functions precmd_functions
preexec_functions=(_mentor_preexec ${preexec_functions:#_mentor_preexec})
precmd_functions=(_mentor_precmd ${precmd_functions:#_mentor_precmd})

# Extra Neovim instances started from this shell also get the session plugin.
if [[ -n $_mentor_nvim_rtp ]]; then
  # The plugin folder is passed in the environment: `set rtp^=<path>` breaks on a space in the path.
  nvim() {
    CLI_MENTOR_SOCK=$_mentor_sock CLI_MENTOR_TOKEN_FILE=$_mentor_token_file CLI_MENTOR_NVIM_RTP=$_mentor_nvim_rtp \
      command nvim --cmd "lua vim.opt.rtp:prepend(vim.env.CLI_MENTOR_NVIM_RTP)" \
      --cmd "lua require('cli_mentor').start()" "$@"
  }
fi

# Announce this shell. Best effort: if the tutor is not listening yet, skip it
# without using a sequence number (nothing the learner did was missed).
() {
  [[ -S $_mentor_sock ]] || return 0
  local d; _mentor_json "$PWD"; d=$REPLY
  _mentor_send "\"type\":\"shell.session_started\",\"shell\":\"zsh\",\"pid\":$$,\"pane\":\"${TMUX_PANE:-}\",\"cwd\":\"$d\""
}
