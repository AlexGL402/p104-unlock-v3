# Remaining Windows RetrainBoth stage: NO-GO

Scope: PR #1, GPU0 0000:03:00.0 / Root 0000:00:02.0 only.
Decision: **NO-GO for an exact Windows RetrainBoth hardware runner.**
No executor or device operations added. Existing tools unchanged.

## Current evidence and intended baseline

Operator reports ApplyLiveGroup GROUP PASS, GPU LNKCAP2=6, both TLS=2,
BAR0 SPEED=20045800 and VSEC 88610=1, physical Gen1 x4.
The most recent admitted 8C1C0 variant is 00120032.
These are operator observations, not a fresh device inspection by this change.
GROUP PASS and capability declarations do not establish physical Gen2.

## Why the remaining Windows write is blocked

[RetrainBoth](../src/P104Gen2x4.cs#L586) reads GPU capability location,
then reads and writes Root Link Control with bit5 set, then reads and writes
GPU Link Control with bit5 set. It has no intervening Training/DLLLA poll,
timeout, AER gate or result check for PciWrite.
WriteTlsBoth is separate; the already confirmed TLS=2 needs no repetition.

PCI Express Base Specification Revision 2.1, section 7.8.7, Link Control
Register, bit5, explicitly makes Retrain Link reserved for Endpoints,
PCIe-to-PCI/PCI-X bridges and Switch Upstream Ports:
[PCI-SIG specification hosted by Intel](https://www.intel.com/content/dam/support/us/en/programmable/support-resources/fpga-wiki/asset03/pci-express-base-r2.1.pdf).
The same restriction appears in
[Microsoft WDK RetrainLink](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/ns-ntddk-_pci_express_link_control_register#members).
GP104 here is an endpoint. Possession of Link Control does not make every
field applicable. No cited GP104-specific source establishes an exception
that makes this reserved bit a supported endpoint retrain command.
A successful config write or zero readback would not establish one.

There is a second exact-port problem: Windows PciRead/PciWrite use four-byte
buffers at capability+10 ([implementation](../src/P104Gen2x4.cs#L412)).
The requested DWORD includes Link Status in its upper half; if passed through
as a DWORD, writing back asserted W1C status bits can clear LBMS/LABS.
The C# interface alone does not prove the driver's final transaction width.
A guarded 16-bit Linux write avoids that effect but is not an exact DWORD
replay. Neither width provides documented endpoint RL semantics.

Root-first training also makes immediate endpoint access unsafe under the
required Root guards. Waiting for Root Training=0/DLLLA=1 before GPU access
would be a necessary safety deviation, not evidence that the GPU write works.

## Disposition

Do not start a Root retrain and only then discover the endpoint step is
inadmissible. No TLS, SPEED, CHANGE_SPEED, MMIO, reset, width-arm, driver
operation, retries or GPU1 access are authorized by this decision.
No Root-only substitute is presented as RetrainBoth. No hardware launch
command is provided for a blocked executor.

The specific missing prerequisite is authoritative GP104/P104 documentation
or an existing independently captured working transaction trace identifying a
supported meaning, access width and ordering for endpoint Link Control bit5.
An existing trace must distinguish actual endpoint register writes from a
driver ignoring the request; Windows final Gen2 alone cannot attribute success
to that endpoint write. Without such evidence, another reserved-bit write
would not be an evidence-based final reproduction attempt.

Safe command to view this decision (no PCI/sysfs access):

```sh
cat docs/linux-gen2-retrain-both-decision.md
```
