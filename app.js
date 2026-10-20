// app.js
const express = require("express");
const { connect } = require("puppeteer-real-browser");
const crypto = require('crypto');

const turnstile = require("./turnstile");
const interstitial = require("./interstitial");

const app = express();
const port = process.env.PORT || 7860;

app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// ── LIMIT & CLEANER CONFIG ──
const MAX_ACTIVE = Number(process.env.MAX_ACTIVE || 5); // Batasi 5 browser paralel
const MAX_CONTEXTS = 20; // Rotasi browser setelah 20 context
const TASK_TTL_MS = 2 * 60 * 1000; // Hapus task setelah 2 menit

let activeTasks = 0;
const tasks = {};

let browser;
let browserStarting;
let contextCounter = 0;
let isResetting = false;

// CLEANER: Hapus task lama dari memori setiap 1 menit
setInterval(() => {
    const now = Date.now();
    for (const [id, task] of Object.entries(tasks)) {
        if (task.createdAt && (now - task.createdAt > TASK_TTL_MS)) {
            delete tasks[id];
            console.log(`[CLEANER] Task ${id} expired and removed.`);
        }
    }
}, 60000);

async function init_browser(proxyObj) {
    console.log('[BROWSER] init_browser called');

    if (!browser || !browser.isConnected()) {
        browser = null;
        if (!browserStarting) {
            console.log("--- [BROWSER] Instance started ---");
            browserStarting = connect({
                headless: false, 
                turnstile: true,
                connectOption: {
                    defaultViewport: null,
                    protocolTimeout: 120000 
                },
                disableXvfb: true, 
                args: [
                    "--no-sandbox",
                    "--display=:99",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox"
                ],
            }).then(ctx => {
                browser = ctx.browser;
                console.log('[BROWSER] Browser connected');
                browser.on('disconnected', () => {
                    console.log('[BROWSER] Browser disconnected');
                    browser = null;
                    browserStarting = null; 
                });
                return browser;
            }).catch(err => {
                console.error("[BROWSER] Launch error:", err.message);
                browserStarting = null;
                throw { usr: "ERROR_CAPTCHA_UNSOLVABLE", api: `launch error: ${err.message}` };
            });
        }
        await browserStarting;
    }

    try {
        console.log('[BROWSER] Creating context with proxy:', proxyObj?.server || 'none');
        const contextOptions = proxyObj?.server ? { proxyServer: proxyObj.server } : {};
        const context = await browser.createBrowserContext(contextOptions);
        const page = await context.newPage();
        
        if (proxyObj?.username) {
            await page.authenticate({ username: proxyObj.username, password: proxyObj.password });
        }
        
        page.setDefaultNavigationTimeout(60000);
        return { page, context }; 
    } catch (err) {
        console.error('[BROWSER] Context error:', err.message);
        throw { usr: "ERROR_CAPTCHA_UNSOLVABLE", api: `ctx error: ${err.message}` };
    }
}

async function reset_browser() {
    if (isResetting) return;
    isResetting = true;
    browserStarting = null; 
    if (browser) {
        await browser.close().catch(() => {});
        browser = null;
    }
    contextCounter = 0;
    console.log("--- [BROWSER] Instance closed & rotated ---");
    isResetting = false;
}

// ── ROUTER ──
app.get("/", (req, res) => {
    res.status(200).json({ status: "ok", message: "ready", uptime: `${Math.floor(process.uptime())}s`, active: activeTasks });
});

app.post("/solve", async (req, res) => {
    const body = req.body;
    const reqTaskId = body.taskId;

    // 1. Jika client mengirim taskId, balas dengan status task tersebut
    if (reqTaskId) {
        const existingTask = tasks[reqTaskId];
        if (!existingTask) {
            return res.status(200).json({
                status: "error",
                taskId: reqTaskId,
                err: { usr: "TASK_NOT_FOUND", api: "Task ID invalid or expired.", msg: "Task expired." }
            });
        }
        return res.status(200).json(existingTask);
    }

    // 2. Validasi Request Baru
    if (body.method !== "CloudflareTask" || typeof body.task !== "object") {
        return res.status(400).json({ status: "error", err: { usr: "BAD_PARAMETERS", api: "Invalid method.", msg: "Provide valid params." } });
    }

    const task = body.task;
    const method = (task.type || "").toLowerCase();
    const domain = task.websiteUrl;
    const siteKey = task.websiteKey;

    if (method !== "turnstile" && method !== "interstitial") {
        return res.status(400).json({ status: "error", err: { usr: "BAD_PARAMETERS", api: "Type not supported.", msg: "Turnstile/Interstitial only." } });
    }

    // 3. Batas Browser
    if (activeTasks >= MAX_ACTIVE) {
        return res.status(429).json({
            status: "error",
            err: { usr: "TOO_MANY_REQUESTS", api: "Server overload.", msg: "Max active capacity reached." }
        });
    }

    // 4. Proses Task
    const newTaskId = crypto.randomUUID().replace(/-/g, '').slice(0, 12);
    activeTasks++;
    tasks[newTaskId] = { status: "processing", taskId: newTaskId, createdAt: Date.now() };
    
    res.status(202).json({ taskId: newTaskId, status: "pending" });
        
    (async () => {
        let ctx;     
        try {
            if (contextCounter >= MAX_CONTEXTS) await reset_browser();

            ctx = await init_browser(task.proxy || null);
            contextCounter++;

            let result;
            if (method === "turnstile") {
                // Memanggil turnstile.js (Injection)
                result = await turnstile({ domain, siteKey }, ctx.page);
            } else {
                result = await interstitial({ domain, payload: task.payload || null }, ctx.page); 
            }

            const solution = {};
            if (method === "turnstile") {
                solution.token = result.token;
            } else {
                solution.cf_clearance = result.solution?.clearance || "";
                solution.user_agent = result.solution?.user_agent || "";
            }

            tasks[newTaskId] = { 
                status: "ready", 
                taskId: newTaskId,
                solution: solution,
                createdAt: Date.now()
            };

        } catch (err) {
            console.error(`[Task ${newTaskId}] Solver error:`, err);
            tasks[newTaskId] = {
                status: "error",
                taskId: newTaskId,
                err: { usr: err.usr || "ERROR_CAPTCHA_UNSOLVABLE", api: err.api || err.message || String(err) } 
            };
        } finally {
            activeTasks--;
            setTimeout(async () => {
                try {
                    if (ctx) {
                        if (ctx.page) await ctx.page.close().catch(() => {});
                        if (ctx.context) await ctx.context.close().catch(() => {});
                    }
                } catch (err) { /* ignore */ }
            }, 5000); // Beri waktu 5 detik sebelum context dihancurkan
        }
    })();
});

// ── GLOBAL ERROR HANDLER ──
app.use((err, req, res, next) => {
    console.error(err.stack);
    res.status(500).json({ status: "error", err: { usr: "INTERNAL_SERVER_ERROR", api: err.message } });
});

app.listen(port, '0.0.0.0', () => {
    console.log(`Server running: http://0.0.0.0:${port}`);
});

const cleanup = async (code) => {
    if (browser) await browser.close().catch(() => {});
    process.exit(code);
};

process.on("SIGINT", () => cleanup(1));
process.on("SIGTERM", () => cleanup(9));
