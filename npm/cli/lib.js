/**
 * `npx @cautonwong/testplatform` 的实现:找 Python → 建 venv → 装包内 wheel → 转发调用。
 *
 * 三条硬规矩(票 09f):
 *   1. **没有 postinstall**:只有真正跑命令时才建环境;之后复用 `.testplatform/`。
 *   2. 失败分三类,消息不同:没网 / Python 缺失或 < 3.10 / 其他。**不抛 ENOENT 堆栈**。
 *   3. 建 venv 与装包各一行进度;超过 60 s 没有输出就打一行"仍在进行"。
 *
 * 这里不做任何业务逻辑:`run` / `gate` / `scaffold` 只是把参数递给 Python 入口。
 */
const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const MIN_PYTHON = [3, 10];
const STATE_DIR = ".testplatform";
const HEARTBEAT_MS = 60_000;

const MESSAGES = {
  pythonMissing: (detail) =>
    `找不到可用的 Python(${detail})。\n` +
    `装一个 Python ≥ 3.10,或用 TESTPLATFORM_PYTHON 指到它。`,
  pythonOld: (version) =>
    `Python ${version} 太旧了,平台要 ≥ 3.10。\n` +
    `换一个 ≥ 3.10 的解释器,或用 TESTPLATFORM_PYTHON 指到它。`,
  noNetwork: (detail) =>
    `装平台包时连不上包源(可能是没网/代理)。\n${detail}\n` +
    `联网后重跑同一条命令即可(已建好的 venv 会复用)。`,
  wheelTampered: (detail) =>
    `包内 wheel 和清单里记的指纹不一致(可能是下载/传输途中被改过)。
${detail}
` +
    `不要重试着装:先确认包来源,再用 npm cache clean --force 后重装。`,
  pipFailed: (detail) =>
    `装平台包失败(pip 报错,不是网络问题)。\n${detail}\n` +
    `先看上面那行 pip 输出;要清干净就删掉项目下的 ${STATE_DIR}/ 再重跑。`,
};

class ShellError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

function stateDir(projectDir) {
  return path.join(projectDir, STATE_DIR);
}

function venvPython(projectDir) {
  const base = path.join(stateDir(projectDir), "venv");
  return process.platform === "win32"
    ? path.join(base, "Scripts", "python.exe")
    : path.join(base, "bin", "python");
}

function heartbeat(label) {
  const timer = setInterval(() => console.error(`${label}:仍在进行…`), HEARTBEAT_MS);
  timer.unref?.();
  return () => clearInterval(timer);
}

function spawnChecked(command, args, options) {
  const result = spawnSync(command, args, { encoding: "utf8", ...options });
  if (result.error) {
    // ENOENT 这类底层错误:**不要原样抛**(用户看到的是堆栈,不是"该装 Python")
    throw new ShellError("pythonMissing", MESSAGES.pythonMissing(result.error.code || result.error.message));
  }
  return result;
}

function pythonVersion(python) {
  const result = spawnSync(python, ["--version"], { encoding: "utf8" });
  if (result.error) {
    return null;
  }
  const text = `${result.stdout || ""}${result.stderr || ""}`;
  const match = text.match(/(\d+)\.(\d+)/);
  if (!match) {
    return null;
  }
  return { major: Number(match[1]), minor: Number(match[2]), text: match[0] };
}

function findPython(env) {
  const candidates = [env.TESTPLATFORM_PYTHON, "python3", "python"].filter(Boolean);
  for (const candidate of candidates) {
    const version = pythonVersion(candidate);
    if (version) {
      return { python: candidate, version };
    }
  }
  throw new ShellError("pythonMissing", MESSAGES.pythonMissing("python3 / python 都跑不起来"));
}

function bundledWheel(pkgDir) {
  const distDir = path.join(pkgDir, "dist");
  const wheels = fs.existsSync(distDir)
    ? fs.readdirSync(distDir).filter((name) => name.endsWith(".whl"))
    : [];
  if (wheels.length !== 1) {
    throw new ShellError(
      "pipFailed",
      `包内没有 wheel(${distDir})—— 发行包的 dist/ 应该带一个。\n` +
        `本地开发时先跑 npm run prepack。`,
    );
  }
  return path.join(distDir, wheels[0]);
}

/**
 * 装之前先比对 wheel 的 sha256:`package.json` 里那份是 prepack 当场算的。
 * 不一致 = 包被动过,直接停 —— 装一个"内容不明的平台"比装失败危险得多。
 */
function verifyWheel(wheelPath) {
  const fingerprintPath = path.join(path.dirname(wheelPath), "wheel.json");
  let expected;
  try {
    expected = JSON.parse(fs.readFileSync(fingerprintPath, "utf8")).sha256;
  } catch (error) {
    throw new ShellError("wheelTampered", `读不到 ${fingerprintPath}(prepack 没跑过?)`);
  }
  if (!expected) {
    throw new ShellError("wheelTampered", `${fingerprintPath} 里没有 sha256`);
  }
  const actual = crypto.createHash("sha256").update(fs.readFileSync(wheelPath)).digest("hex");
  if (actual !== expected) {
    throw new ShellError(
      "wheelTampered",
      `期望 ${expected}
实际 ${actual}
文件 ${path.basename(wheelPath)}`,
    );
  }
  return actual;
}

function pythonFor(projectDir, env, pkgDir, log) {
  const target = venvPython(projectDir);
  if (fs.existsSync(target) && !env.TESTPLATFORM_FORCE_INSTALL) {
    return target; // 复用:第二次起不再建 venv、不再装包
  }
  if (env.TESTPLATFORM_SKIP_ENV === "1" && env.TESTPLATFORM_PYTHON) {
    // 已经在本项目环境里装好平台的人可以跳过建 venv(也能让"转发"这条被单测)
    return env.TESTPLATFORM_PYTHON;
  }

  const { python, version } = findPython(env);
  checkPythonVersion(version);

  log(`正在建虚拟环境:${stateDir(projectDir)}`);
  const stopVenv = heartbeat("建虚拟环境");
  const venv = spawnChecked(python, ["-m", "venv", path.join(stateDir(projectDir), "venv")], {
    stdio: "inherit",
  });
  stopVenv();
  if (venv.status !== 0) {
    throw new ShellError("pipFailed", MESSAGES.pipFailed("python -m venv 失败(见上面的输出)"));
  }

  const wheel = bundledWheel(pkgDir);
  verifyWheel(wheel);
  log(`正在安装平台(包内 wheel):${path.basename(wheel)}`);
  const stopPip = heartbeat("安装平台");
  const pip = spawnSync(target, ["-m", "pip", "install", "--quiet", wheel], { encoding: "utf8" });
  stopPip();
  if (pip.error) {
    throw new ShellError("pythonMissing", MESSAGES.pythonMissing(pip.error.code || pip.error.message));
  }
  if (pip.status !== 0) {
    const detail = `${pip.stdout || ""}${pip.stderr || ""}`.trim().split("\n").slice(-3).join("\n");
    const networkish = /Could not find a version|No matching distribution|Network is unreachable|Temporary failure|ProxyError|timed out|Failed to establish/i.test(detail);
    throw new ShellError(
      networkish ? "noNetwork" : "pipFailed",
      networkish ? MESSAGES.noNetwork(detail) : MESSAGES.pipFailed(detail),
    );
  }
  return target;
}

function ensureGitignoreHint(projectDir, log) {
  const entry = `${STATE_DIR}/`;
  const gitignore = path.join(projectDir, ".gitignore");
  const current = fs.existsSync(gitignore) ? fs.readFileSync(gitignore, "utf8") : "";
  if (current.split(/\r?\n/).includes(entry)) {
    return;
  }
  fs.appendFileSync(gitignore, current.endsWith("\n") || !current ? `${entry}\n` : `\n${entry}\n`);
  log(`已把 ${entry} 加进 .gitignore(虚拟环境不进版本库)`);
}

/** 转发一次 Python 调用;返回同样的退出码。 */
function forward(python, args, projectDir) {
  const result = spawnSync(python, args, { stdio: "inherit", cwd: projectDir });
  if (result.error) {
    throw new ShellError("pythonMissing", MESSAGES.pythonMissing(result.error.code || result.error.message));
  }
  return result.status === null ? 1 : result.status;
}

/** 每条子命令 → Python 入口的参数。纯函数,测试直接打它。 */
function pythonArgs(command, rest) {
  switch (command) {
    case "run":
      return ["-m", "testplatform.execution.cli", ...rest];
    case "gate":
      return ["-m", "testplatform.execution.gate", ...rest];
    case "scaffold":
      return ["-m", "testplatform.cli", "scaffold", ...rest];
    case "eject":
      return ["-m", "testplatform.cli", "eject", ...rest];
    case "init":
      return null; // init 只准备环境,不调平台
    default:
      return null;
  }
}

/** 版本不够 → 一类专门的失败(消息里点名要 ≥ 3.10)。 */
function checkPythonVersion(version) {
  if (
    version.major < MIN_PYTHON[0] ||
    (version.major === MIN_PYTHON[0] && version.minor < MIN_PYTHON[1])
  ) {
    throw new ShellError("pythonOld", MESSAGES.pythonOld(version.text));
  }
  return version;
}

/** pip 失败分两类:没网 vs 别的。纯函数,测试直接打它。 */
function classifyPipFailure(detail) {
  return /Could not find a version|No matching distribution|Network is unreachable|Temporary failure|ProxyError|timed out|Failed to establish/i.test(
    detail || "",
  )
    ? "noNetwork"
    : "pipFailed";
}

module.exports = {
  classifyPipFailure,
  checkPythonVersion,
  HEARTBEAT_MS,
  MESSAGES,
  STATE_DIR,
  ShellError,
  bundledWheel,
  ensureGitignoreHint,
  findPython,
  forward,
  pythonArgs,
  pythonFor,
  stateDir,
  verifyWheel,
  venvPython,
};
