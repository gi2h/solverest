/**
 * API_TURNSTILE
 */
async function turnstile({ domain, siteKey, action, cdata, pagedata }, page) {
    const timeout = 20000;

    try {
        await page.setDefaultNavigationTimeout(timeout);

        const htmlContent = `
            <!DOCTYPE html>
            <html lang="en">
            <body>
                <div class="turnstile"></div>
                <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback" defer></script>
                <script>
                    window.onloadTurnstileCallback = function () {
                        turnstile.render('.turnstile', {
                            sitekey: '${siteKey}',
                            ${action ? `action: '${action}',` : ""}
                            ${cdata ? `cData: '${cdata}',` : ""}
                            ${pagedata ? `chlPageData: '${pagedata}',` : ""}
                            callback: function (token) {
                                var c = document.createElement('input');
                                c.type = 'hidden';
                                c.name = 'cf-response';
                                c.value = token;
                                document.body.appendChild(c);
                            }
                        });
                    };
                </script>
            </body>
            </html>
        `;

        await page.setRequestInterception(true);
        page.removeAllListeners("request");
        page.on("request", async (request) => {
            try {
                if (
                    [domain, domain + "/"].includes(request.url()) &&
                    request.resourceType() === "document"
                ) {
                    await request.respond({
                        status: 200,
                        contentType: "text/html",
                        body: htmlContent
                    });
                } else {
                    await request.continue();
                }
            } catch {}
        });

        await page.goto(domain, { waitUntil: "domcontentloaded" });

        await page.waitForSelector('[name="cf-response"]', { timeout });

        const token = await page.evaluate(() => {
            const el = document.querySelector('[name="cf-response"]');
            return el ? el.value : null;
        });

        if (!token || token.length < 10) {
            throw new Error("totally failed");
        }

        return {
            token,
            type: "turnstile"
        };

    } catch (e) {
        throw { 
            usr: "ERROR_CAPTCHA_UNSOLVABLE", 
            api: e.message || String(e) 
        };
    } finally {
        try {
            await page.setRequestInterception(false);
        } catch {}
    }
}

module.exports = turnstile;