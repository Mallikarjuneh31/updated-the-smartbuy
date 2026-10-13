/**
 * SmartBuy — Main JavaScript
 * ===========================
 * No hardcoded brand presets or category mappings.
 * Handles: scroll animations, navbar effects, autocomplete,
 * buy redirect gate, and table filter.
 */

let pendingRedirectUrl = null;
let isUserSignedIn = false;

document.addEventListener("DOMContentLoaded", () => {

    // ── 1. Intersection Observer for scroll animations ──────────────────────
    const scrollObserver = new IntersectionObserver((entries, observer) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add("animated");
                observer.unobserve(entry.target);
            }
        });
    }, { root: null, rootMargin: "0px", threshold: 0.12 });

    document.querySelectorAll(".scroll-anim").forEach(el => scrollObserver.observe(el));

    // ── 2. Sticky navbar scroll effect ─────────────────────────────────────
    const navbar = document.querySelector(".navbar-glass");
    if (navbar) {
        window.addEventListener("scroll", () => {
            navbar.classList.toggle("navbar-scrolled", window.scrollY > 20);
        }, { passive: true });
    }

    // ── 3. Sign Up gate form handler ───────────────────────────────────────
    const signupForm = document.getElementById("signupForm");
    if (signupForm) {
        signupForm.addEventListener("submit", (e) => {
            e.preventDefault();
            isUserSignedIn = true;
            const modal = bootstrap.Modal.getInstance(
                document.getElementById("signupModal")
            );
            if (modal) modal.hide();
            if (pendingRedirectUrl) {
                window.open(pendingRedirectUrl, "_blank");
                pendingRedirectUrl = null;
            }
        });
    }

    // ── 4. Show loading overlay on all search form submissions ──────────────
    document.querySelectorAll("form[action='/search']").forEach(form => {
        form.addEventListener("submit", () => {
            const overlay = document.getElementById("loadingOverlay");
            if (overlay) {
                overlay.classList.remove("d-none");
                overlay.classList.add("d-flex");
            }
        });
    });

    // ── 5. Initialize autocomplete on all inputs with .autocomplete-input ──
    initAutocomplete();

    // ── 6. Category chip click → prefill search bar ────────────────────────
    document.querySelectorAll(".category-chip").forEach(chip => {
        chip.addEventListener("click", (e) => {
            const searchInput = document.getElementById("mainSearchInput");
            if (searchInput && chip.dataset.query) {
                e.preventDefault();
                searchInput.value = chip.dataset.query;
                searchInput.focus();
                // Auto-submit after brief delay
                setTimeout(() => {
                    document.getElementById("mainSearchForm")?.submit();
                }, 100);
            }
        });
    });
});


// ── Buy Redirect Gate ─────────────────────────────────────────────────────
function triggerBuyRedirect(element) {
    if (!element) return;
    const targetLink = element.getAttribute("data-url") || "";
    const platform = element.getAttribute("data-platform") || "";
    const productName = element.getAttribute("data-product-name") || "";
    const price = element.getAttribute("data-price") || "";
    const rating = element.getAttribute("data-rating") || "";
    const reviews = element.getAttribute("data-reviews") || "";
    const image = element.getAttribute("data-image") || "";
    const specifications = element.getAttribute("data-specifications") || "";

    handleBuyRedirect(targetLink, platform, productName, price, rating, reviews, image, specifications);
}

function handleBuyRedirect(targetLink, platform = '', productName = '', price = '', rating = '', reviews = '', image = '', specifications = '') {
    let platformLower = (platform || '').toLowerCase();
    let buyEndpoint = '/deal/redirect';
    if (platformLower.includes('amazon')) {
        buyEndpoint = '/buy/amazon';
    } else if (platformLower.includes('flipkart')) {
        buyEndpoint = '/buy/flipkart';
    } else if (platformLower.includes('meesho')) {
        buyEndpoint = '/buy/meesho';
    } else if (platformLower.includes('gec')) {
        buyEndpoint = '/buy/gec';
    }

    let redirectUrl = buyEndpoint + '?url=' + encodeURIComponent(targetLink || '') +
        '&platform=' + encodeURIComponent(platform || '') +
        '&product_name=' + encodeURIComponent(productName || '') +
        '&price=' + encodeURIComponent(price || '') +
        '&rating=' + encodeURIComponent(rating || '') +
        '&reviews=' + encodeURIComponent(reviews || '') +
        '&image=' + encodeURIComponent(image || '') +
        '&specifications=' + encodeURIComponent(specifications || '');
    window.location.href = redirectUrl;
}


// ── Table Filter (black comparison table) ─────────────────────────────────
function filterTable() {
    const input = document.getElementById("tableFilter");
    if (!input) return;
    const filter = input.value.toLowerCase();
    document.querySelectorAll(".comparison-table-card tbody tr").forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(filter) ? "" : "none";
    });
}


// ── Autocomplete Engine ───────────────────────────────────────────────────
function initAutocomplete() {
    document.querySelectorAll(".autocomplete-input").forEach(input => {
        const form      = input.closest("form");
        const container = input.closest(".position-relative") || form;
        const dropdown  = container?.querySelector(".autocomplete-dropdown");

        if (!dropdown) return;

        let debounceTimer = null;
        let selectedIndex = -1;

        // Input event — debounced fetch after 2 characters
        input.addEventListener("input", () => {
            clearTimeout(debounceTimer);
            const val = input.value.trim();
            if (!val || val.length < 2) {
                hideDropdown(dropdown);
                return;
            }
            debounceTimer = setTimeout(() => {
                fetch(`/api/search-suggestions?q=${encodeURIComponent(val)}`)
                    .then(r => r.json())
                    .then(data => {
                        const list = Array.isArray(data) ? data : (data.suggestions || []);
                        renderSuggestions(list, dropdown, input, form);
                        selectedIndex = -1;
                    })
                    .catch(() => {});
            }, 120);
        });

        // Show trending/initial suggestions on focus
        input.addEventListener("focus", () => {
            const val = input.value.trim();
            if (val.length >= 2) {
                fetch(`/api/search-suggestions?q=${encodeURIComponent(val)}`)
                    .then(r => r.json())
                    .then(data => {
                        const list = Array.isArray(data) ? data : (data.suggestions || []);
                        if (list.length > 0) {
                            renderSuggestions(list, dropdown, input, form);
                        }
                    })
                    .catch(() => {});
            }
        });

        // Keyboard navigation (Up, Down, Enter, Escape)
        input.addEventListener("keydown", (e) => {
            const items = dropdown.querySelectorAll(".autocomplete-item");
            if (!items.length || dropdown.classList.contains("d-none")) return;

            if (e.key === "ArrowDown") {
                e.preventDefault();
                selectedIndex = (selectedIndex + 1) % items.length;
                updateActiveItem(items, selectedIndex);
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                selectedIndex = (selectedIndex - 1 + items.length) % items.length;
                updateActiveItem(items, selectedIndex);
            } else if (e.key === "Enter" && selectedIndex >= 0) {
                e.preventDefault();
                items[selectedIndex].click();
            } else if (e.key === "Escape") {
                hideDropdown(dropdown);
                selectedIndex = -1;
            }
        });

        // Close on outside click
        document.addEventListener("click", (e) => {
            if (container && !container.contains(e.target)) {
                hideDropdown(dropdown);
                selectedIndex = -1;
            }
        });
    });
}

function hideDropdown(dropdown) {
    dropdown.innerHTML = "";
    dropdown.classList.add("d-none");
}

function updateActiveItem(items, index) {
    items.forEach((item, i) => {
        item.classList.toggle("active", i === index);
        if (i === index) item.scrollIntoView({ block: "nearest" });
    });
}

function getCategoryIcon(term) {
    const t = String(term).toLowerCase();
    if (t.includes("phone") || t.includes("mobile") || t.includes("iphone") || t.includes("samsung") || t.includes("pixel")) return "fas fa-mobile-alt text-primary";
    if (t.includes("laptop") || t.includes("macbook")) return "fas fa-laptop text-info";
    if (t.includes("watch")) return "fas fa-clock text-warning";
    if (t.includes("shoe") || t.includes("sneaker") || t.includes("sandal")) return "fas fa-shoe-prints text-success";
    if (t.includes("shirt") || t.includes("dress") || t.includes("saree") || t.includes("jean")) return "fas fa-tshirt text-danger";
    if (t.includes("milk") || t.includes("rice") || t.includes("oil") || t.includes("sugar") || t.includes("tea") || t.includes("bread") || t.includes("egg") || t.includes("cheese")) return "fas fa-shopping-basket text-success";
    if (t.includes("soap") || t.includes("shampoo") || t.includes("wash") || t.includes("cream") || t.includes("lotion")) return "fas fa-pump-soap text-primary";
    if (t.includes("lipstick") || t.includes("makeup") || t.includes("kajal") || t.includes("mascara")) return "fas fa-magic text-danger";
    if (t.includes("baby") || t.includes("diaper")) return "fas fa-baby text-info";
    if (t.includes("dog") || t.includes("cat") || t.includes("pet")) return "fas fa-paw text-warning";
    if (t.includes("book") || t.includes("novel")) return "fas fa-book text-primary";
    if (t.includes("chair") || t.includes("table") || t.includes("sofa") || t.includes("bed")) return "fas fa-couch text-secondary";
    return "fas fa-search text-muted";
}

function renderSuggestions(suggestions, dropdown, input, form) {
    dropdown.innerHTML = "";
    if (!suggestions?.length) {
        hideDropdown(dropdown);
        return;
    }

    suggestions.forEach((title, idx) => {
        const item = document.createElement("div");
        item.className = "autocomplete-item";
        item.setAttribute("role", "option");

        const icon = document.createElement("i");
        icon.className = getCategoryIcon(title) + " me-2";
        item.appendChild(icon);

        const text = document.createElement("span");
        text.textContent = title;
        item.appendChild(text);

        item.addEventListener("click", () => {
            input.value = title;
            hideDropdown(dropdown);
            const overlay = document.getElementById("loadingOverlay");
            if (overlay) {
                overlay.classList.remove("d-none");
                overlay.classList.add("d-flex");
            }
            if (form) form.submit();
        });

        dropdown.appendChild(item);
    });

    dropdown.classList.remove("d-none");
}


// ── Dynamic Product Specifications Categories Engine ──────────────────────
const SPEC_CATEGORIES = {
    "Mobiles": {
        placeholder: "Enter Product Name (optional, e.g. Samsung Galaxy S23)...",
        fields: [
            { id: "spec_brand", name: "brand", label: "Brand", icon: "fas fa-dot-circle", iconColor: "#2563eb", options: ["All Brands", "Apple", "Samsung", "Xiaomi", "OnePlus", "Realme", "Vivo", "Oppo", "Poco", "Motorola"] },
            { id: "spec_price", name: "price_range", label: "Price Range", icon: "fas fa-tag", iconColor: "#16a34a", options: [
                { text: "Any Price", val: "" },
                { text: "Under ₹10,000", val: "0-10000" },
                { text: "₹10,000 – ₹20,000", val: "10000-20000" },
                { text: "₹20,000 – ₹40,000", val: "20000-40000" },
                { text: "₹40,000 – ₹70,000", val: "40000-70000" },
                { text: "Above ₹70,000", val: "70000-999999" }
            ]},
            { id: "spec_ram", name: "ram", label: "RAM", icon: "fas fa-microchip", iconColor: "#d97706", options: ["Any RAM", "4 GB", "6 GB", "8 GB", "12 GB", "16 GB"] },
            { id: "spec_storage", name: "storage", label: "Storage", icon: "fas fa-hdd", iconColor: "#0891b2", options: ["Any Storage", "64 GB", "128 GB", "256 GB", "512 GB", "1 TB"] },
            { id: "spec_processor", name: "processor", label: "Processor", icon: "fas fa-cpu", iconColor: "#dc2626", options: ["Any Processor", "Snapdragon", "MediaTek Dimensity", "Apple A-Series", "Exynos", "Unisoc"] },
            { id: "spec_battery", name: "battery", label: "Battery Capacity", icon: "fas fa-battery-full", iconColor: "#16a34a", options: ["Any Battery", "4000+ mAh", "5000+ mAh", "6000+ mAh"] },
            { id: "spec_display", name: "display", label: "Display Size", icon: "fas fa-desktop", iconColor: "#2563eb", options: ["Any Display Size", 'Under 6.1"', '6.1" - 6.5"', '6.6" - 6.8"', 'Above 6.8"'] },
            { id: "spec_color", name: "color", label: "Color", icon: "fas fa-palette", iconColor: "#64748b", options: ["Any Color", "Black", "Blue", "Silver", "White", "Green", "Gold", "Purple"] },
            { id: "spec_rating", name: "min_rating", label: "Minimum Rating", icon: "fas fa-star", iconColor: "#eab308", options: [
                { text: "Any Rating", val: "" },
                { text: "3.0★ & above", val: "3.0" },
                { text: "3.5★ & above", val: "3.5" },
                { text: "4.0★ & above", val: "4.0" },
                { text: "4.5★ & above", val: "4.5" }
            ]}
        ]
    },
    "Laptops": {
        placeholder: "Enter Product Name (optional, e.g. MacBook Air M2)...",
        fields: [
            { id: "spec_brand", name: "brand", label: "Brand", icon: "fas fa-dot-circle", iconColor: "#2563eb", options: ["All Brands", "Apple", "HP", "Dell", "Lenovo", "Asus", "Acer", "MSI", "Samsung"] },
            { id: "spec_price", name: "price_range", label: "Price Range", icon: "fas fa-tag", iconColor: "#16a34a", options: [
                { text: "Any Price", val: "" },
                { text: "Under ₹30,000", val: "0-30000" },
                { text: "₹30,000 – ₹50,000", val: "30000-50000" },
                { text: "₹50,000 – ₹80,000", val: "50000-80000" },
                { text: "₹80,000 – ₹1,20,000", val: "80000-120000" },
                { text: "Above ₹1,20,000", val: "120000-999999" }
            ]},
            { id: "spec_ram", name: "ram", label: "RAM", icon: "fas fa-microchip", iconColor: "#d97706", options: ["Any RAM", "8 GB", "16 GB", "32 GB", "64 GB"] },
            { id: "spec_storage", name: "storage", label: "Storage", icon: "fas fa-hdd", iconColor: "#0891b2", options: ["Any Storage", "256 GB SSD", "512 GB SSD", "1 TB SSD", "2 TB SSD"] },
            { id: "spec_processor", name: "processor", label: "Processor", icon: "fas fa-cpu", iconColor: "#dc2626", options: ["Any Processor", "Intel Core i3", "Intel Core i5", "Intel Core i7", "Intel Core i9", "AMD Ryzen 5", "AMD Ryzen 7", "Apple M1/M2/M3"] },
            { id: "spec_battery", name: "battery", label: "Battery Life", icon: "fas fa-battery-full", iconColor: "#16a34a", options: ["Any Battery", "4-6 Hours", "6-10 Hours", "10+ Hours"] },
            { id: "spec_display", name: "display", label: "Display Size", icon: "fas fa-laptop", iconColor: "#2563eb", options: ["Any Display Size", '13.3"', '14.0"', '15.6"', '16.0"', '17.3"'] },
            { id: "spec_color", name: "color", label: "Color", icon: "fas fa-palette", iconColor: "#64748b", options: ["Any Color", "Space Grey", "Silver", "Black", "Dark Blue"] },
            { id: "spec_rating", name: "min_rating", label: "Minimum Rating", icon: "fas fa-star", iconColor: "#eab308", options: [
                { text: "Any Rating", val: "" },
                { text: "3.0★ & above", val: "3.0" },
                { text: "3.5★ & above", val: "3.5" },
                { text: "4.0★ & above", val: "4.0" },
                { text: "4.5★ & above", val: "4.5" }
            ]}
        ]
    },
    "Headphones": {
        placeholder: "Enter Product Name (optional, e.g. Sony WH-1000XM5)...",
        fields: [
            { id: "spec_brand", name: "brand", label: "Brand", icon: "fas fa-dot-circle", iconColor: "#2563eb", options: ["All Brands", "Sony", "JBL", "boAt", "Bose", "Apple", "Noise", "Sennheiser", "Realme", "OnePlus"] },
            { id: "spec_price", name: "price_range", label: "Price Range", icon: "fas fa-tag", iconColor: "#16a34a", options: [
                { text: "Any Price", val: "" },
                { text: "Under ₹1,000", val: "0-1000" },
                { text: "₹1,000 – ₹3,000", val: "1000-3000" },
                { text: "₹3,000 – ₹7,000", val: "3000-7000" },
                { text: "₹7,000 – ₹15,000", val: "7000-15000" },
                { text: "Above ₹15,000", val: "15000-999999" }
            ]},
            { id: "spec_type", name: "type", label: "Headphone Type", icon: "fas fa-headphones", iconColor: "#d97706", options: ["Any Type", "Truly Wireless (TWS)", "In-Ear Wired", "Over-Ear Wireless", "On-Ear"] },
            { id: "spec_anc", name: "anc", label: "Noise Cancellation", icon: "fas fa-volume-mute", iconColor: "#0891b2", options: ["Any Feature", "Active Noise Cancellation (ANC)", "Environmental Noise Cancellation (ENC)", "Passive Isolation"] },
            { id: "spec_connectivity", name: "connectivity", label: "Connectivity", icon: "fas fa-wifi", iconColor: "#dc2626", options: ["Any Connectivity", "Bluetooth 5.3", "Bluetooth 5.2", "Wired 3.5mm", "Type-C"] },
            { id: "spec_battery", name: "battery", label: "Battery Playtime", icon: "fas fa-battery-full", iconColor: "#16a34a", options: ["Any Battery", "Up to 15 hrs", "15-30 hrs", "30-50 hrs", "50+ hrs"] },
            { id: "spec_mic", name: "mic", label: "Microphone", icon: "fas fa-microphone", iconColor: "#2563eb", options: ["Any Mic", "Built-in Mic", "Dual Mic", "Quad Mic"] },
            { id: "spec_color", name: "color", label: "Color", icon: "fas fa-palette", iconColor: "#64748b", options: ["Any Color", "Black", "White", "Blue", "Red", "Beige"] },
            { id: "spec_rating", name: "min_rating", label: "Minimum Rating", icon: "fas fa-star", iconColor: "#eab308", options: [
                { text: "Any Rating", val: "" },
                { text: "3.0★ & above", val: "3.0" },
                { text: "3.5★ & above", val: "3.5" },
                { text: "4.0★ & above", val: "4.0" },
                { text: "4.5★ & above", val: "4.5" }
            ]}
        ]
    },
    "Smartwatches": {
        placeholder: "Enter Product Name (optional, e.g. Apple Watch Series 9)...",
        fields: [
            { id: "spec_brand", name: "brand", label: "Brand", icon: "fas fa-dot-circle", iconColor: "#2563eb", options: ["All Brands", "Apple", "Samsung", "Noise", "Fire-Boltt", "boAt", "Amazfit", "Fastrack", "Titan", "Garmin"] },
            { id: "spec_price", name: "price_range", label: "Price Range", icon: "fas fa-tag", iconColor: "#16a34a", options: [
                { text: "Any Price", val: "" },
                { text: "Under ₹2,000", val: "0-2000" },
                { text: "₹2,000 – ₹5,000", val: "2000-5000" },
                { text: "₹5,000 – ₹10,000", val: "5000-10000" },
                { text: "₹10,000 – ₹25,000", val: "10000-25000" },
                { text: "Above ₹25,000", val: "25000-999999" }
            ]},
            { id: "spec_display", name: "display", label: "Display Type", icon: "fas fa-desktop", iconColor: "#d97706", options: ["Any Display", "AMOLED", "HD LCD", "Retina Display", "TFT"] },
            { id: "spec_shape", name: "shape", label: "Dial Shape", icon: "fas fa-shapes", iconColor: "#0891b2", options: ["Any Shape", "Square", "Round", "Rectangle"] },
            { id: "spec_features", name: "features", label: "Features", icon: "fas fa-heartbeat", iconColor: "#dc2626", options: ["Any Feature", "Bluetooth Calling", "GPS Tracking", "Heart Rate Monitor", "SpO2 & ECG", "Sports Modes"] },
            { id: "spec_battery", name: "battery", label: "Battery Life", icon: "fas fa-battery-full", iconColor: "#16a34a", options: ["Any Battery", "Up to 3 Days", "3-7 Days", "7-14 Days", "14+ Days"] },
            { id: "spec_strap", name: "strap", label: "Strap Material", icon: "fas fa-band-aid", iconColor: "#2563eb", options: ["Any Strap", "Silicone", "Leather", "Stainless Steel", "Nylon"] },
            { id: "spec_color", name: "color", label: "Color", icon: "fas fa-palette", iconColor: "#64748b", options: ["Any Color", "Black", "Silver", "Rose Gold", "Blue", "Green"] },
            { id: "spec_rating", name: "min_rating", label: "Minimum Rating", icon: "fas fa-star", iconColor: "#eab308", options: [
                { text: "Any Rating", val: "" },
                { text: "3.0★ & above", val: "3.0" },
                { text: "3.5★ & above", val: "3.5" },
                { text: "4.0★ & above", val: "4.0" },
                { text: "4.5★ & above", val: "4.5" }
            ]}
        ]
    },
    "Home Appliances": {
        placeholder: "Enter Product Name (optional, e.g. LG Washing Machine)...",
        fields: [
            { id: "spec_brand", name: "brand", label: "Brand", icon: "fas fa-dot-circle", iconColor: "#2563eb", options: ["All Brands", "LG", "Samsung", "Whirlpool", "Bosch", "Haier", "IFB", "Godrej", "Philips", "Havells"] },
            { id: "spec_price", name: "price_range", label: "Price Range", icon: "fas fa-tag", iconColor: "#16a34a", options: [
                { text: "Any Price", val: "" },
                { text: "Under ₹10,000", val: "0-10000" },
                { text: "₹10,000 – ₹25,000", val: "10000-25000" },
                { text: "₹25,000 – ₹50,000", val: "25000-50000" },
                { text: "₹50,000 – ₹80,000", val: "50000-80000" },
                { text: "Above ₹80,000", val: "80000-999999" }
            ]},
            { id: "spec_appliance", name: "appliance_type", label: "Appliance Type", icon: "fas fa-blender", iconColor: "#d97706", options: ["Any Appliance", "Washing Machine", "Refrigerator", "Air Conditioner", "Microwave", "Air Fryer", "Water Purifier"] },
            { id: "spec_star", name: "star_rating", label: "Star Rating", icon: "fas fa-certificate", iconColor: "#0891b2", options: ["Any Star Rating", "3 Star", "4 Star", "5 Star"] },
            { id: "spec_capacity", name: "capacity", label: "Capacity", icon: "fas fa-box", iconColor: "#dc2626", options: ["Any Capacity", "Small (1-2 members)", "Medium (3-4 members)", "Large (5+ members)"] },
            { id: "spec_tech", name: "tech", label: "Technology", icon: "fas fa-bolt", iconColor: "#16a34a", options: ["Any Tech", "Inverter Technology", "Direct Cool", "Frost Free", "Smart WiFi"] },
            { id: "spec_warranty", name: "warranty", label: "Warranty", icon: "fas fa-shield-alt", iconColor: "#2563eb", options: ["Any Warranty", "1 Year", "2 Years", "5+ Years Compressor"] },
            { id: "spec_color", name: "color", label: "Color", icon: "fas fa-palette", iconColor: "#64748b", options: ["Any Color", "Stainless Steel", "Black", "Silver", "White", "Floral Red"] },
            { id: "spec_rating", name: "min_rating", label: "Minimum Rating", icon: "fas fa-star", iconColor: "#eab308", options: [
                { text: "Any Rating", val: "" },
                { text: "3.0★ & above", val: "3.0" },
                { text: "3.5★ & above", val: "3.5" },
                { text: "4.0★ & above", val: "4.0" },
                { text: "4.5★ & above", val: "4.5" }
            ]}
        ]
    }
};

function switchSpecCategory(catName, btnEl) {
    // 1. Update active category pill UI
    document.querySelectorAll(".spec-cat-pill").forEach(b => b.classList.remove("active"));
    if (btnEl) {
        btnEl.classList.add("active");
    } else {
        const matchingBtn = document.querySelector(`.spec-cat-pill[data-cat='${catName}']`);
        if (matchingBtn) matchingBtn.classList.add("active");
    }

    // 2. Set hidden category input
    const catInput = document.getElementById("specCategoryInput");
    if (catInput) catInput.value = catName;

    const data = SPEC_CATEGORIES[catName];
    if (!data) return;

    // 3. Update search input placeholder
    const searchInput = document.getElementById("specSearchInput");
    if (searchInput && data.placeholder) {
        searchInput.placeholder = data.placeholder;
    }

    // 4. Render 9 fields grid
    const grid = document.getElementById("specFieldsGrid");
    if (!grid) return;

    grid.innerHTML = "";
    data.fields.forEach(field => {
        const col = document.createElement("div");
        col.className = "col-12 col-md-6 col-lg-4";

        let optionsHTML = "";
        field.options.forEach(opt => {
            if (typeof opt === "object") {
                optionsHTML += `<option value="${opt.val}">${opt.text}</option>`;
            } else {
                const val = (opt.startsWith("All") || opt.startsWith("Any")) ? "" : opt;
                optionsHTML += `<option value="${val}">${opt}</option>`;
            }
        });

        col.innerHTML = `
            <div class="spec-field-box">
                <label class="spec-field-label">
                    <i class="${field.icon}" style="color: ${field.iconColor};"></i>
                    <span>${field.label}</span>
                </label>
                <select name="${field.name}" id="${field.id}" class="form-select spec-field-select">
                    ${optionsHTML}
                </select>
            </div>
        `;
        grid.appendChild(col);
    });
}



