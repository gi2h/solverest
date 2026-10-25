// app.js

const express = require("express");
const { connect } = require("puppeteer-real-browser");
const crypto = require('crypto');

// Ubah dari "./Api/turnstile" menjadi "./turnstile"
const turnstile = require("./turnstile");

// Ubah dari "./Api/interstitial" menjadi "./interstitial"
const interstitial = require("./interstitial");

const app = express();
const port = process.env.PORT || 7860;

app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Concurrency
const MAX_ACTIVE = Number(process.env.MAX_ACTIVE || 10);
let activeTasks = 0;
const tasks = {};

let browser;
let browserStarting;
let contextCounter = 0;
const MAX_CONTEXTS = 25;
let isResetting = false;

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
                throw {
                    usr: "ERROR_CAPTCHA_UNSOLVABLE",
                    api: `launch error: ${err.message}` 
                };
            });
        }
        await browserStarting;
    }

    try {
        console.log('[BROWSER] Creating context with proxy:', proxyObj?.server || 'none');
        const contextOptions = proxyObj?.server ? { proxyServer: proxyObj.server } : {};
        const context = await browser.createBrowserContext(contextOptions);
        console.log('[BROWSER] Context created');
        
        const page = await context.newPage();
        console.log('[BROWSER] Page created');
        
        if (proxyObj?.username) {
            console.log('[BROWSER] Setting proxy authentication...');
            await page.authenticate({ 
                username: proxyObj.username, 
                password: proxyObj.password 
            });
            console.log('[BROWSER] Proxy auth set');
        }
        
        page.setDefaultNavigationTimeout(60000);
        return { page, context }; 
    } catch (err) {
        console.error('[BROWSER] Context error:', err.message);
        console.error('[BROWSER] Stack:', err.stack);
        throw {
            usr: "ERROR_CAPTCHA_UNSOLVABLE",
            api: `ctx error: ${err.message}` 
        };
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

// Router
app.get("/", (req, res) => {
    const uptime = process.uptime();
    res.status(200).json({
        status: "ok",
        message: "ready",
        uptime: `${Math.floor(uptime)}s`
    });
});

app.post("/solve", async (req, res) => {
    const body = req.body;
    const reqTaskId = body.taskId;

    if (reqTaskId) {
        const existingTask = tasks[reqTaskId];
        if (!existingTask) {
            return res.status(200).json({
                status: "error",
                taskId: reqTaskId,
                err: {
                    usr: "TASK_NOT_FOUND",
                    api: "Requested taskId does not exist in memory.",
                    msg: "The task ID you provided is invalid or has expired."
                }
            });
        }
        return res.status(200).json(existingTask);
    }

    if (body.method !== "CloudflareTask" || typeof body.task !== "object") {
        return res.status(400).json({
            status: "error",
            err: {
                usr: "BAD_PARAMETERS",
                api: "Root parameter 'method' must be 'CloudflareTask' and 'task' must be an object.",
                msg: "Please provide valid parameters."
            }
        });
    }

    const task = body.task;

    const method = (task.type || "").toLowerCase();
    const domain = task.websiteUrl;
    const siteKey = task.websiteKey;
    const action = task.action;
    const cdata = task.cdata;
    const pagedata = task.pdata || task.pagedata;
    const payload = task.payload || null;
    const rawProxy = task.proxy;

    if (method !== "turnstile" && method !== "interstitial") {
        return res.status(400).json({
            status: "error",
            err: {
                usr: "BAD_PARAMETERS",
                api: "Task parameter 'type' is missing or not supported. Supported: turnstile, interstitial",
                msg: "Please provide valid parameters."
            }
        });
    }

    if (method === "turnstile" && (!domain || !siteKey)) {
        return res.status(400).json({
            status: "error",
            err: {
                usr: "BAD_PARAMETERS",
                api: "Method 'turnstile' requires both 'websiteUrl' and 'websiteKey'.",
                msg: "Please provide valid parameters."
            }
        });
    }

    if (method === "interstitial" && !domain) {
        return res.status(400).json({
            status: "error",
            err: {
                usr: "BAD_PARAMETERS",
                api: "Method 'interstitial' requires parameter 'websiteUrl'.",
                msg: "Please provide valid parameters."
            }
        });
    }

    try {
        const checkUrl = new URL(domain);
        if (!['http:', 'https:'].includes(checkUrl.protocol)) {
            throw new Error("Protocol must be http:// or https://");
        }
    } catch (e) {
        return res.status(400).json({
            status: "error",
            err: { 
                usr: "BAD_PARAMETERS", 
                api: `Invalid 'websiteUrl': ${e.message}`,
                msg: "Please provide a valid URL."
            }
        });
    }

    if (activeTasks >= MAX_ACTIVE) {
        return res.status(429).json({
            status: "error",
            err: {
                usr: "TOO_MANY_REQUESTS",
                api: "Server cluster overload, max active capacity reached.",
                msg: "Too many requests, please wait a moment."
            }
        });
    }

    let proxyObj = null;
    if (rawProxy) {
        try {
            if (typeof rawProxy === 'object' && rawProxy.host && rawProxy.port) {
                const pType = rawProxy.type || 'http';
                const auth = (rawProxy.user && rawProxy.pass) ? `${rawProxy.user}:${rawProxy.pass}@` : "";
                const proxyString = `${pType}://${auth}${rawProxy.host}:${rawProxy.port}`;
                const u = new URL(proxyString);
                proxyObj = { 
                    server: `${u.protocol}//${u.host}`, 
                    username: u.username, 
                    password: u.password 
                };
            } else if (typeof rawProxy === 'string') {
                const u = new URL(rawProxy);
                proxyObj = { 
                    server: `${u.protocol}//${u.host}`, 
                    username: u.username, 
                    password: u.password 
                };
            }
        } catch (e) {
            return res.status(400).json({
                status: "error",
                err: { 
                    usr: "BAD_PARAMETERS", 
                    api: `Proxy parse error: ${e.message}`, 
                    msg: "Invalid proxy configuration." 
                }
            });
        }
    }

    const newTaskId = crypto.randomUUID().replace(/-/g, '').slice(0, 12);
    activeTasks++;
    tasks[newTaskId] = { status: "processing", taskId: newTaskId };
    
    res.status(202).json({
        taskId: newTaskId,
        status: "pending" 
    });
        
    (async () => {
        let ctx;     
        try {
            if (contextCounter >= MAX_CONTEXTS) {
                await reset_browser();
            }

            ctx = await init_browser(proxyObj);
            contextCounter++;

            let result;
            if (method === "turnstile") {
                result = await turnstile({ domain, siteKey, action, cdata, pagedata }, ctx.page);
            } else if (method === "interstitial") {
                result = await interstitial({ domain, payload }, ctx.page); 
            }

            const solution = {};
            if (method === "turnstile") {
                solution.token = result.token;
            } else if (method === "interstitial") {
                solution.cf_clearance = result.solution?.clearance || "";
                solution.user_agent = result.solution?.user_agent || "";
                if (result.solution?.__cf_bm) {
                    solution.__cf_bm = result.solution.__cf_bm;
                }
            }

            tasks[newTaskId] = { 
                status: "ready", 
                taskId: newTaskId,
                solution: solution
            };
            
            setTimeout(() => { delete tasks[newTaskId]; }, 120000);

        } catch (err) {
            console.error(`[Task ${newTaskId}] Solver error:`, err);

            const usr = err.usr || "ERROR_CAPTCHA_UNSOLVABLE";
            const api = err.api || err.message || String(err);
            
            tasks[newTaskId] = {
                status: "error",
                taskId: newTaskId,
                err: {
                    usr: usr,
                    api: api,
                    msg: usr === "ERROR_CAPTCHA_UNSOLVABLE" ? "Internal solver error occurred, please try again later" : "Solver encountered an error."
                } 
            };

            setTimeout(() => { delete tasks[newTaskId]; }, 10000);

        } finally {
            activeTasks--;
            
            setTimeout(async () => {
                try {
                    if (ctx) {
                        if (ctx.page) await ctx.page.close().catch(() => {});
                        if (ctx.context) {
                            await ctx.context.close().catch(() => {});
                        }
                    }
                } catch (err) {
                    console.error(`[Task ${newTaskId}] cleanup failed`, err.message);
                }
            }, 8000);
        }
    })();
});

app.use((err, req, res, next) => {
    console.error(err.stack);
    res.status(500).json({
        status: "error",
        err: {
            usr: "INTERNAL_SERVER_ERROR",
            api: err.message || "Express global crash intercept",
            msg: "An unexpected error occurred on the server."
        }
    });
});

app.listen(port, '0.0.0.0', () => {
    console.log(`Server running: http://0.0.0.0:${port}`);
});

const cleanup = async (code) => {
    console.log(`(code: ${code}). Cleaning up...`);
    if (browser) {
        try { await browser.close(); } catch (e) { console.error("Error closing browser:", e.message); }
    }
    process.exit(code);
};

process.on("SIGINT", () => cleanup(1));
process.on("SIGTERM", () => cleanup(9));