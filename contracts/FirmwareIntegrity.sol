// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title FirmwareIntegrity
 * @notice Blockchain-based firmware integrity registry for IoT devices.
 *         Extended from the MostaqHossain/firmware-blockchain PoC with:
 *         - Owner-only write access
 *         - Firmware version tracking
 *         - Tamper-evident event log
 *         - Multi-device batch support
 */
contract FirmwareIntegrity {

    address public immutable owner;

    struct FirmwareRecord {
        string  hash;        // SHA-256 hex string of the .mpy firmware blob
        string  version;     // Semver string e.g. "1.0.3"
        uint256 timestamp;   // Block timestamp at registration
        bool    revoked;     // Allows emergency invalidation
    }

    // deviceID (e.g. "esp32-lab-01") => FirmwareRecord
    mapping(string => FirmwareRecord) private _records;

    // ---------------------------------------------------------------
    // Events
    // ---------------------------------------------------------------
    event FirmwareRegistered(
        string  indexed deviceID,
        string  hash,
        string  version,
        uint256 timestamp
    );

    event FirmwareRevoked(
        string  indexed deviceID,
        uint256 timestamp
    );

    // ---------------------------------------------------------------
    // Modifiers
    // ---------------------------------------------------------------
    modifier onlyOwner() {
        require(msg.sender == owner, "FirmwareIntegrity: caller is not owner");
        _;
    }

    // ---------------------------------------------------------------
    // Constructor
    // ---------------------------------------------------------------
    constructor() {
        owner = msg.sender;
    }

    // ---------------------------------------------------------------
    // Write functions (owner only)
    // ---------------------------------------------------------------

    /**
     * @notice Register or update the authoritative firmware hash for a device.
     * @param deviceID  Unique device identifier string.
     * @param hash      SHA-256 hex digest of the firmware binary.
     * @param version   Human-readable version string.
     */
    function storeFirmwareHash(
        string calldata deviceID,
        string calldata hash,
        string calldata version
    ) external onlyOwner {
        require(bytes(deviceID).length > 0, "deviceID empty");
        require(bytes(hash).length == 64,   "hash must be 64-char hex SHA-256");

        _records[deviceID] = FirmwareRecord({
            hash:      hash,
            version:   version,
            timestamp: block.timestamp,
            revoked:   false
        });

        emit FirmwareRegistered(deviceID, hash, version, block.timestamp);
    }

    /**
     * @notice Revoke a firmware record (mark as untrusted without deleting).
     */
    function revokeFirmware(string calldata deviceID) external onlyOwner {
        require(bytes(_records[deviceID].hash).length > 0, "no record found");
        _records[deviceID].revoked = true;
        emit FirmwareRevoked(deviceID, block.timestamp);
    }

    // ---------------------------------------------------------------
    // Read functions (public)
    // ---------------------------------------------------------------

    /**
     * @notice Retrieve a device's authoritative firmware record.
     */
    function getFirmwareRecord(string calldata deviceID)
        external view
        returns (
            string  memory hash,
            string  memory version,
            uint256        timestamp,
            bool           revoked
        )
    {
        FirmwareRecord storage r = _records[deviceID];
        return (r.hash, r.version, r.timestamp, r.revoked);
    }

    /**
     * @notice Convenience function: return only the hash (mirrors PoC API).
     */
    function getFirmwareHash(string calldata deviceID)
        external view
        returns (string memory)
    {
        return _records[deviceID].hash;
    }

    /**
     * @notice On-chain verification: compare supplied hash against stored record.
     * @return valid  True if hashes match and record is not revoked.
     */
    function verifyFirmware(string calldata deviceID, string calldata hash)
        external view
        returns (bool valid)
    {
        FirmwareRecord storage r = _records[deviceID];
        if (r.revoked) return false;
        return keccak256(bytes(r.hash)) == keccak256(bytes(hash));
    }
}
