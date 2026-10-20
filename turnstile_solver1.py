/**
 * API_TURNSTILE (v2 - Dynamic & Robust)
 */
async function turnstile({ domain, siteKey, action, cdata, pagedata }, page) {
    // Naikkan timeout karena Cloudflare kadang butuh waktu render
    const timeout = 30000;

    try {
        await page.setDefaultNavigationTimeout(timeout);

        // Injeksi HTML dengan error-callback agar kita tahu jika sitekey ditolak
        const htmlContent = `
            <!DOCTYPE html>
            <html lang="en">
            <head><meta charset="utf-8"></head>
            <body style="background:#fff; display:flex; justify-content:center; align-items:center; height:100vh;">
                <div class="turnstile"></div>
                <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback" async defer></script>
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
                            },
                            'error-callback': function (err) {
                                // Tangkap error dari Cloudflare
                                var c = document.createElement('input');
                                c.type = 'hidden';
                                c.name = 'cf-error';
                                c.value = String(err);
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
                // Cek jika URL yang diminta persis sama dengan domain target
                const isTarget = request.url() === domain || request.url() === domain + "/";
                
                if (isTarget && request.resourceType() === "document") {
                    await request.respond({
                        status: 200,
                        contentType: "text/html",
                        body: htmlContent
                    });
                } else {
                    await request.continue();
                }
            } catch (e) {
                // Abaikan error jika request sudah dibatalkan
            }
        });

        // Buka domain (ini akan memicu request interception di atas)
        await page.goto(domain, { waitUntil: "domcontentloaded" });

        // Tunggu sampai token muncul ATAU error muncul
        await page.waitForSelector('[name="cf-response"], [name="cf-error"]', { timeout });

        // Ambil hasilnya
        const result = await page.evaluate(() => {
            const tokenEl = document.querySelector('[name="cf-response"]');
            const errorEl = document.querySelector('[name="cf-error"]');
            
            if (tokenEl && tokenEl.value) return { token: tokenEl.value };
            if (errorEl && errorEl.value) return { error: errorEl.value };
            return null;
        });

        if (result && result.error) {
            throw new Error(`CF Error: ${result.error}`);
        }

        if (!result || !result.token || result.token.length < 10) {
            throw new Error("Token tidak valid atau kosong");
        }

        return {
            token: result.token,
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
