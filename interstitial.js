/**
 * API_INTERSTITIAL
 */
async function interstitial({ domain, payload = null }, page) {
    const timeout = 60000;
    const startTime = Date.now();

    try {
        console.log(`[INTERSTITIAL] Domain: ${domain}, Payload: ${payload ? 'ADA' : 'TIDAK ADA'}`);

        await page.goto(domain, { waitUntil: "domcontentloaded", timeout: 50000 }).catch(() => {});

        if (payload) {
            console.log('[INTERSTITIAL] Sending POST');

            const body = new URLSearchParams(payload).toString();
            console.log('[INTERSTITIAL] Payload body:', body);

            const result = await page.evaluate(async (url, postData) => {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: postData
                });
                
                const html = await resp.text();
                const cookieHeader = resp.headers.get('set-cookie') || '';
                const cfMatch = cookieHeader.match(/cf_clearance=([^;]+)/);
                const bmMatch = cookieHeader.match(/__cf_bm=([^;]+)/);
                
                return {
                    status: resp.status,
                    html: html,
                    cf_clearance: cfMatch ? cfMatch[1] : null,
                    __cf_bm: bmMatch ? bmMatch[1] : null,
                    cookieHeader: cookieHeader
                };
            }, domain, body);

            console.log('[INTERSTITIAL] POST status:', result.status);

            await page.setContent(result.html, { waitUntil: 'networkidle0' });

            if (result.cf_clearance) {
                await page.setCookie({
                    name: 'cf_clearance',
                    value: result.cf_clearance,
                    domain: new URL(domain).hostname,
                    path: '/',
                    httpOnly: false,
                    secure: true,
                    sameSite: 'Lax'
                });
            }
            if (result.__cf_bm) {
                await page.setCookie({
                    name: '__cf_bm',
                    value: result.__cf_bm,
                    domain: new URL(domain).hostname,
                    path: '/',
                    httpOnly: true,
                    secure: true,
                    sameSite: 'Lax'
                });
            }
        } else {
            console.log('[INTERSTITIAL] GET only (no payload)');
        }

        let lastLog = 0;
        while (Date.now() - startTime < timeout) {
            try {
                const cookies = await page.cookies();
                const cfClearance = cookies.find(c => c.name === 'cf_clearance');

                if (Date.now() - lastLog > 5000) {
                    console.log(`[INTERSTITIAL] Waiting... cf_clearance: ${cfClearance?.value ? 'YES' : 'NO'}`);
                    lastLog = Date.now();
                }

                if (cfClearance?.value) {
                    const isStillOnChallengePage = await page.evaluate(() => {
                        const title = document.title.toLowerCase();
                        if (title.includes("just a moment")) return true;
                        if (document.querySelector("#challenge-running, #challenge-stage")) return true;
                        const bodyText = document.body ? document.body.innerText.toLowerCase() : '';
                        return bodyText.includes("checking your browser") || 
                               bodyText.includes("cf-browser-verification");
                    }).catch(() => false);

                    if (!isStillOnChallengePage) {
                        const currentUA = await page.evaluate(() => navigator.userAgent).catch(() => "");
                        const cfBmCookie = cookies.find(c => c.name === '__cf_bm');
                        
                        console.log('[INTERSTITIAL] Challenge resolved, cf_clearance obtained!');
                        
                        return {
                            status: "done",
                            type: 'interstitial',
                            solution: {
                                clearance: cfClearance.value,
                                user_agent: currentUA,
                                __cf_bm: cfBmCookie ? cfBmCookie.value : null
                            }
                        };
                    }
                }
            } catch (e) {
                if (e.message.includes("Target closed") || e.message.includes("Session closed") || e.message.includes("detached")) {
                    throw new Error("ctx closed unexpectedly");
                }
                console.warn("[INTERSTITIAL] Minor cycle warning:", e.message);
            }
            
            const elapsed = Date.now() - startTime;
            const delay = elapsed < 10000 ? 1500 : elapsed < 25000 ? 2000 : 2500;
            await new Promise(resolve => setTimeout(resolve, delay));
        }

        throw new Error("timeout error");

    } catch (err) {
        console.error('[INTERSTITIAL] Error:', err.message);
        throw {
            usr: "ERROR_CAPTCHA_UNSOLVABLE",
            api: err.message || String(err)
        };
    }
}

module.exports = interstitial;