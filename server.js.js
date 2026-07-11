// ... existing code ...
const express = require('express');
const axios = require('axios');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

const app = express();
const PORT = 3000;
// ... existing code ...
// Store active incidents to send to the frontend
let activeIncidents = [];

// PRO-TIP: Geocoding Cache. 
// Load and process your intersections.json file
let geoCache = {};
const cacheFilePath = path.join(__dirname, 'intersections.json');

/**
 * Normalizes and loads your JSON into a format the map understands
 */
function loadAndProcessCache() {
    try {
        if (fs.existsSync(cacheFilePath)) {
            const rawData = fs.readFileSync(cacheFilePath, 'utf8');
            const data = JSON.parse(rawData);
            
            let count = 0;
            for (const [key, coords] of Object.entries(data)) {
                // If it's a valid coordinate array [lat, lng]
                if (Array.isArray(coords) && coords.length === 2) {
                    // Normalize the key: remove "street:" prefix if it exists
                    const cleanKey = key.replace('street:', '').trim();
                    
                    geoCache[cleanKey] = {
                        lat: coords[0],
                        lng: coords[1]
                    };
                    count++;
                }
            }
            console.log(`[Cache] Successfully loaded and normalized ${count} locations.`);
        } else {
            console.log(`[Cache] No intersections.json found.`);
        }
    } catch (err) {
        console.error(`[Cache Error] Failed to process intersections.json:`, err.message);
    }
}

// Initial load
loadAndProcessCache();

// Helper function to save new coordinates to file
function saveCacheToFile() {
    // Only save the updated cache back to a clean JSON structure
    fs.writeFileSync(cacheFilePath, JSON.stringify(geoCache, null, 2));
}

/**
 * Converts an intersection into Latitude/Longitude coordinates
 */
async function geocodeLocation(location) {
    // Check for exact match (e.g., "YONGE ST")
    if (geoCache[location]) {
        return geoCache[location];
    }
    
    // Fallback: Try searching for the first part of the intersection if it's like "A / B"
    const primary = location.split('/')[0].trim();
    if (geoCache[primary]) {
        return geoCache[primary];
    }

let geoCache = {};
const cacheFilePath = path.join(__dirname, 'intersections.json');

try {
    if (fs.existsSync(cacheFilePath)) {
        const fileData = fs.readFileSync(cacheFilePath, 'utf8');
        geoCache = JSON.parse(fileData);
        console.log(`[Cache] Loaded ${Object.keys(geoCache).length} pre-geocoded intersections from file.`);
    } else {
        console.log(`[Cache] No intersections.json found. Starting fresh.`);
    }
} catch (err) {
    console.error(`[Cache Error] Could not load intersections.json:`, err.message);
}

// Helper function to save new coordinates back to the file so it learns
function saveCacheToFile() {
    fs.writeFileSync(cacheFilePath, JSON.stringify(geoCache, null, 2));
}

/**
 * Converts an intersection into Latitude/Longitude coordinates
 * @param {string} location - e.g., "YONGE ST / DUNDAS ST E"
 */
async function geocodeLocation(location) {
    // 1. Check if we already know this location (from your JSON file)
    if (geoCache[location]) {
// ... existing code ...
        // 4. Extract data and cache it
        if (response.data && response.data.length > 0) {
            const coords = {
                lat: parseFloat(response.data[0].lat),
                lng: parseFloat(response.data[0].lon)
            };
            geoCache[location] = coords; // Save to memory
            saveCacheToFile();           // Append it to your JSON file!
            return coords;
        } else {
// ... existing code ...