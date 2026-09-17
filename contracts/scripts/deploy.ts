import { ethers, network } from "hardhat";
import * as fs from "fs";
import * as path from "path";

/**
 * LOCALHOST-ONLY dev-loop deployer: EventfulDataEdge for subgraph development.
 *
 * This is the contract half of `subgraph/scripts/dev.sh` — it exists so a
 * fresh `npx hardhat node` gives the subgraph something to index. It is NOT
 * a live-network deployment path: live deploys go through the Foundry
 * scripts (`script/DeployDataEdgeStaging.s.sol` pattern — pinned CREATE2
 * address, broadcast, hand-edited registry), per the Phase 3 deployment
 * plan §2/§3.
 *
 * History (#650 cleanup): the old version of this script deployed to any
 * network and `fs.writeFileSync`'d the WHOLE `deployed-addresses.json`,
 * destroying the `stagingGnosis` block on every run (deploy-plan §2.5).
 * Restored for the dev loop only, with a merge-write into a dedicated
 * `local` block that can never touch the live-network records.
 *
 * Usage:
 *   npx hardhat run scripts/deploy.ts --network localhost
 */

interface LocalDeployRecord {
  network: string;
  chainId: number;
  entryPoint: string;
  eventfulDataEdge: string;
  deployedAt: string;
  deployer: string;
  blockNumber: number;
}

async function main() {
  if (network.name !== "hardhat" && network.name !== "localhost") {
    throw new Error(
      `scripts/deploy.ts is the localhost dev-loop deployer (got network '${network.name}'). ` +
        `Live-network deploys use the Foundry scripts — see docs/plans/2026-08-02-phase3-contract-deployment-plan.md §2/§3.`,
    );
  }

  const [deployer] = await ethers.getSigners();
  console.log("=== TotalReclaw dev-loop deploy (localhost) ===");
  console.log(`Deployer: ${deployer.address}`);

  // The subgraph dev flow (dev.sh + tests/e2e-ombh-validation.ts) expects
  // `local.entryPoint` to equal the dev deployer (the ombh E2E asserts it).
  const entryPointAddr = deployer.address;

  console.log("Deploying EventfulDataEdge...");
  const EdgeFactory = await ethers.getContractFactory("EventfulDataEdge");
  const edge = await EdgeFactory.deploy();
  await edge.waitForDeployment();
  const edgeAddr = await edge.getAddress();
  console.log(`  Address: ${edgeAddr}`);

  const chainId = (await ethers.provider.getNetwork()).chainId;
  const blockNumber = await ethers.provider.getBlockNumber();

  const record: LocalDeployRecord = {
    network: network.name,
    chainId: Number(chainId),
    entryPoint: entryPointAddr,
    eventfulDataEdge: edgeAddr,
    deployedAt: new Date().toISOString(),
    deployer: deployer.address,
    blockNumber,
  };

  // Merge-write into the `local` block ONLY — never rewrite the whole file,
  // never touch the gnosis/stagingGnosis/historical records.
  const outPath = path.join(__dirname, "..", "deployed-addresses.json");
  const existing = fs.existsSync(outPath)
    ? JSON.parse(fs.readFileSync(outPath, "utf8"))
    : {};
  existing.local = record;
  fs.writeFileSync(outPath, JSON.stringify(existing, null, 2) + "\n");
  console.log(`Local deploy recorded to deployed-addresses.json -> local`);

  // ABI copy for the subgraph (dev.sh also does this; keep both idempotent).
  const abiSrc = path.join(__dirname, "..", "artifacts", "contracts", "EventfulDataEdge.sol", "EventfulDataEdge.json");
  const abiDstDir = path.join(__dirname, "..", "..", "subgraph", "abis");
  if (!fs.existsSync(abiDstDir)) fs.mkdirSync(abiDstDir, { recursive: true });
  fs.copyFileSync(abiSrc, path.join(abiDstDir, "EventfulDataEdge.json"));
  console.log("ABI copied to subgraph/abis/");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
