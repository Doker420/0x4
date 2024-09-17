*Reverse Engineering 0x4 Fun*
*Examining the user-mode APC injection sensor introduced in Windows 10 build 1809*
`Yesterday I've read Microsoft's blog post about the new ATP kernel sensors added to log injection of user-mode APCs. That got me curious and I went to examine the changes in KeInsertQueueApc.`

The instrumentation code invokes the function EtwTiLogQueueApcThread to log the event. The function's prototype looks like this :
VOID EtwTiLogQueueApcThread(
    BYTE PreviousMode,
    PKTHREAD ApcThread, //Apc->Thread
    PVOID NormalRoutine,
    PVOID NormalContext,
    PVOID SystemArgument1,
    PVOID SystemArgument2);

EtwTiLogQueueApcThread is only called when the queued APC is a user-mode APC and if KeInsertQueueApc successfully inserted the APC into the queue (Thread->ApcQueueable && !Apc->Inserted).

EtwTiLogQueueApcThread first checks whether the user-mode APC has been queued to a remote process or not and only logs the event in the former case.
It also distinguishes between remotely queued APCs from user-mode (NtQueueApcThread(Ex)) and those queued from kernel-mode; The former is used to detect user-mode injection techniques like this one.

As shown below, two event descriptors exist and the one to log is determined using the current thread's previous mode to know whether the APC was queued by a user process or by a kernel driver.



Looking at where the event provider registration handle EtwThreatIntProvRegHandle is referenced, we see that not only remote user-mode APC injection is logged but also a bunch of events that are commonly used by threats.


Thanks for reading and until another time :)

Links:
`https://www.pentesteracademy.com/`
`https://repo.zenk-security.com/Reversing%20.%20cracking/Reversing%20-%20Secrets%20Of%20Reverse%20Engineering%20(2005).pdf`
`https://malwareunicorn.org/workshops/re101.html#0`
`https://0xresetti.github.io/reversing.html`
`https://guyinatuxedo.github.io/`
`https://p.ost2.fyi/courses/course-v1:OpenSecurityTraining2+Arch1001_x86-64_Asm+2021_v1/about`
`https://class.malware.re/`

