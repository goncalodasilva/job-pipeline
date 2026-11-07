# User guide

This guide explains how to use the Job Search Pipeline day to day. It follows the app's areas in the order of the sidebar. To install and start the app, see the [README](../README.md).

Words in **bold** are the labels you see on screen. The examples use fictional people and companies.

The app never submits an application for you. It helps you choose, prepare and keep track. You apply on the employer's site yourself.

## Contents

- [Getting around](#getting-around)
- [Today](#today)
- [Job inbox](#job-inbox)
- [Board and applications](#board-and-applications)
- [Tailoring](#tailoring)
- [Interview prep](#interview-prep)
- [Fact bank](#fact-bank)
- [Enrichment sessions](#enrichment-sessions)
- [Companies](#companies)
- [Settings](#settings)
- [Background tasks and notifications](#background-tasks-and-notifications)
- [Usage budgets](#usage-budgets)
- [The demo](#the-demo)
- [Your data](#your-data)
- [Privacy and honesty](#privacy-and-honesty)
- [Accessibility](#accessibility)

## Getting around

### Sidebar

The sidebar lists the main areas: **Today**, **Job inbox**, **Board**, **Fact bank** and **Companies**, with **Settings** at the bottom. When reminders are due, the Today link says how many. On a narrow screen, press **Menu** in the top bar to show the sidebar.

### Top bar

- **Search** opens the command palette.
- **Tasks** shows how many AI tasks are running or queued. It also says "1 needs attention" when a task failed or was interrupted. Press it to see each task.
- **Notifications** shows how many reminders are new. Press it to see them.

### Quick add and Keyboard shortcuts

At the end of every page, two buttons open the same dialogs as the N and ? keys:

- **Quick add** offers three links: **Add a job** (the Inbox's add form), **Jot a fact** (a new fact for your Fact bank) and **Log an event** (the Board: an event belongs to an application, so you add it from there).
- **Keyboard shortcuts** lists every shortcut.

### Command palette

Press Ctrl+K (Cmd+K on a Mac), or **Search** in the top bar. Type to find jobs, companies, applications and facts. Each word you type matches the start of a word, so "irri tech" finds "Irrigation Technician". Removed jobs and draft or archived facts are included and marked "Removed", "Draft" or "Archived". Choosing a result or an action closes the palette.

Before you type, the palette lists the pages you opened recently (this browser remembers them) and four actions: **Add job**, **New fact**, **Start session** and **New event**. Up and down arrows choose, Enter opens, Esc closes.

### Keyboard shortcuts

| Keys | What they do |
| --- | --- |
| Ctrl+K (Cmd+K on a Mac) | Open the command palette |
| G then T | Go to Today |
| G then I | Go to the Job inbox |
| G then B | Go to the Board |
| G then F | Go to the Fact bank |
| N | Quick add: a job, a fact or an event |
| ? | Show every shortcut |
| Esc | Close a drawer or dialog |

In the Job inbox, J moves to the next job and K to the previous one, A marks a job to apply, R removes it, Enter opens its details, and Z undoes the last move while the Undo line shows.

Single-key shortcuts are ignored while you type in a field. They can clash with speech input or assistive technology, so you can turn them off in Settings > Keyboard shortcuts. Switched off means every single key, ? too. Every action still has a button, Ctrl+K (Cmd+K on a Mac) and Esc keep working, and the **Keyboard shortcuts** button shows the keys that still work and how to turn the single keys back on. The setup guide has no palette and no shortcuts.

## Today

Today answers one question: what needs your attention now. It opens first once setup is done. Blocks with nothing in them are hidden. When nothing is due, it says "Nothing needs your attention today."

The blocks, in order:

- **Upcoming interviews** in the next seven days, each with its prep state: "Prep not started", "Questions ready" or "Practiced for the interview".
- **Interview debriefs**, for an interview that ended in the last 14 days: "How did the interview go? A debrief is optional." Choose **Debrief** or **Not needed**.
- **Due follow-ups and reminders**, each with **Done**, **Snooze** (tomorrow or next week) and **Dismiss**.
- **New jobs**: how many jobs you haven't seen yet, up to three Strong fits, and **Review new jobs**.
- **Drafts to review**: fact drafts, and CVs or cover letters waiting for your approval.
- **Suggested session**, such as "3 questions, about 5 minutes", with **Start session** or **Dismiss**. While a session is open, the block reads **Session in progress**, with **Continue your session**.
- **Finish setting up**: setup steps you skipped.

A line at the bottom counts your applications per status.

## Job inbox

The inbox is where you decide, one job at a time, what you want to apply for.

### Adding a job

1. Press **+ Add job**, or use Quick add or the palette's **Add job**.
2. Paste the posting's address into **Link to the original posting**, or paste the ad into **Or paste the job text**. You can give both.
3. Press **Add job**.

A link is fetched once, when you press the button, with "Fetching the page…" shown while it works. The app reads only that page, never follows links in it, and refuses addresses on your own computer or network. If the page can't be read, for example because it needs JavaScript or a sign-in, the form says so: paste the ad's text instead, and the link is kept as the link to the original. Job text can be up to 50,000 characters.

The job is saved first, so you can triage it even when no AI provider works. Then the app summarizes it and assesses your fit in the background. That uses up to two AI requests.

Adding the same link again adds a source to the job, not a second job. A posting whose text is nearly the same as a job you already have is merged into that job's card, which then says, for example, "also posted on 1 other site". If they are different jobs, press **Not a duplicate**.

### Triage

The **Queue** shows new jobs first, then ones you've seen, beside one focused card. A job counts as seen once its card is shown. On each card:

- **Apply** (A) marks the job "To apply" and creates an application in Preparing on the Board.
- **Remove** (R) asks for an optional reason.
- **Card menu** > **Snooze until**: **Tomorrow**, **Next week**, **In a month**, or a date you pick. The job comes back to the queue as new on that day.
- **Details** (Enter) opens the job's details. **Next** (J) and **Previous** (K) move through the queue.

After a move, an Undo line shows for about ten seconds. Press **Undo** (Z) to take the move back.

Beside the queue are the other views: **Snoozed**, **To apply**, **Removed** and **Hidden by your rules**. A removed job stays in **Removed**. Press **Restore** there to bring it back.

### Removal reasons

When you remove a job, pick a reason or skip it: **Too junior**, **Too senior**, **Location**, **Salary**, **Industry**, **Company**, **Not interested** or **Other**. **Other** opens a box for a few words. Reasons help the app learn what you don't want.

### Job details

Open a job from its title, **Details** or Enter. You see:

- **How you match**: each requirement with the confirmed fact that meets it, or "Missing". Each fact links to your Fact bank.
- The job summary, and **Fields and notes**. Correct any field, such as industry, salary or seniority, and add your notes. A field you corrected is marked "Corrected by you", and a new summary never replaces it.
- **Sources**: every posting merged into this job, each with **Not a duplicate**.
- **Full posting**: the text as captured, with its date and **Open original posting**.

The job is summarized again only when you press **Summarize again**, and its fit only when you press **Assess fit again**.

### How you match

The fit band is **Strong**, **Possible** or **Stretch**. It describes how well your confirmed facts cover the job's requirements, not how good the job is, and it never hides a job. The app works out the band itself: a requirement counts as met only when the AI cites facts that were really sent to it. A job that hits a deal-breaker, or states a salary below your minimum, is flagged "Conflicts with your preferences".

Fit needs at least one confirmed fact. Without one, the job says "Add confirmed facts to see how you match." Treat the band as a guide, not a verdict: the app checks that the cited facts exist, not that every match is right. Read the linked facts yourself.

### Learning from your removals

When several removals share a reason and a detail, for example three "Location" removals of onsite jobs, a suggestion appears at the top of the Queue. It says how many inbox jobs the rule would match:

- **Flag them** marks matching jobs "Matches your rule: …". They stay in the queue.
- **Hide them** moves matching jobs to the **Hidden by your rules** view, which shows a count.
- **Dismiss** removes the suggestion. It comes back only if the evidence for it doubles.

A suggestion appears only when no application, and no job you marked to apply, contradicts it. An accepted rule applies to future jobs too.

Your rules are in Settings > Job preferences > **Removal rules**, with the removals behind each one. There you can switch a rule between **Flag it** and **Hide it**, turn it off, or delete it. You can also change **Minimum matching removals for a suggestion**, which is 3 at first.

Once you've written at least two removal notes, **Group removal notes with AI** sends up to 100 recent notes, with their job IDs, to your AI provider to look for common reasons. What it finds goes through the same suggestion step.

### Filters, grouping and saved views

Open **Filters and grouping** to:

- filter by fit, work mode, industry, seniority and company, or by "Conflicts with preferences";
- search your jobs with **Search jobs**;
- sort by **Newest added**, **Posting date** or **Fit**;
- group by company, industry, fit, work mode or seniority.

Press **Apply filters**, or **Clear filters**. To keep a set of filters, enter a name in **Name this view** and press **Save view**. Saved views can be renamed or deleted.

### Values to map

A summary may hold a value your lists don't know yet, such as a new industry. The inbox then shows "1 new value to map". Open it and choose **Map value** (to an existing value), **Add as new value** or **Ignore**. Fields you corrected are kept.

## Board and applications

### The Board

The **Board** shows each application in a column for its status. Every status belongs to a phase: Preparing, Active or Closed. Closed applications sit in a collapsed **Closed** row; press it to show them. **Table** shows the same applications as a sortable list, 50 to a page.

The built-in statuses are Preparing; Applied, Screening, Interviewing and Offer, which are active; and Rejected, Withdrawn, Accepted and Declined, which are closed. You can rename them, reorder them and add your own in Settings > Lists and statuses.

Each card shows the company and the role, what comes next (the next event, or the last activity), the interview prep state when there is one, and "Follow-up due" when a reminder is due.

### Moving an application

- Press **Move to…** on a card and choose a status. By keyboard: Enter opens the menu, the arrow keys choose, Enter moves and Esc closes.
- On a wider screen you can also drag a card to another column.
- Moving to Interviewing asks "Add the interview date?". Moving to Rejected asks "Any feedback worth noting?" and offers a session about the role's requirements. Both are optional: **Save** or **Skip**.
- Every status change is kept in the application's timeline. The first move to an active status records the applied date and, if you approved a CV or letter, records the latest approved versions as the ones sent. It doesn't submit anything.

### The application drawer and page

**Open application** on a card opens a drawer with the status, the next event, contacts, the documents you sent and the timeline. **Open workspace** opens the application's own page, with three tabs: **Timeline and contacts**, **Tailoring** and **Interview prep**.

### Events and calendar files

Under **Add an event**:

1. Choose the **Event type**: Interview, Follow-up, Deadline or Offer deadline.
2. Give a **Date**, a **Time** and a **Duration (minutes)**, or tick **All day (deadlines only)**.
3. Check the **Time zone**. It starts as your computer's; you can enter another, such as `Europe/Lisbon`.
4. Add the round, format, interviewers, location or link, and details if you like. For an interview, you can tick **Remind me to send a thank-you note**.
5. Press **Save event**.

Each event has **Download calendar file**: an `.ics` file you import into your calendar app. Check its time and zone there. It is a file, not a sync, so later changes in the app don't reach your calendar. **Edit event** and **Cancel event** change the event in the app.

### Reminders

A reminder shows when an application has stayed in a status for the number of days you choose. At first, an application that sits in Applied for 14 days gets "Follow-up due". Change these rules in Settings > Lists and statuses > **Reminders**. Follow-up, deadline and offer deadline events remind you when they are due, and so does a thank-you note you asked for. A thank-you reminder doesn't draft a message.

Reminders show on Today and in Notifications, with **Done**, **Snooze** and **Dismiss**. They work only while the app is open. There are no emails, push notifications or checks while the app is closed.

### Contacts, notes and interactions

- **Contacts** belong to a company. In an application, **Add a contact** (name, role, email, phone, how you know them, notes), or link one of the company's contacts with **Link contact**. **Unlink** removes the link. A job without a company can't have contacts.
- **Add a note** keeps anything worth remembering. Notes and rejection feedback can be edited.
- **Log an interaction** records a date, a channel, a note and, if you like, the contact. Interactions can't be edited yet.

## Tailoring

An application's **Tailoring** tab takes you from the job to the files you send, in five steps. You can go back to any step. Each AI step starts only when you press its button. It sends one request, plus one more if the answer comes back unusable, and counts toward your usage budget.

Above the steps, **CV country** sets the country whose CV conventions this application follows.

### 1. Gap check

**Check gaps** compares the job's requirements with your confirmed facts. Nothing is changed. A requirement counts as shown only when your confirmed facts show it. If must-haves are missing, the app says the role may not be worth applying for.

At a gap, you can **Add a fact** in your own words. It is saved as a confirmed fact; then press **Check again**. Or choose **Answer a few questions about these gaps** to start a session. "Ways to present related experience" suggests honest ways to present what you have, with the facts behind each one.

### 2. Angle

An angle is the one idea your application leads with. **Propose angles** suggests two or three. Press **Use this angle** on one, change it, or **Write your own angle** and **Save angle**. An angle you used before is marked "Used in another application".

### 3. CV

A CV needs a job summary, at least one confirmed fact, a CV country and an approved CV template for that country.

The first time you tailor a CV for a country, draft its template here. **Draft a template for** the country asks the AI for that country's conventions, from your choices: length, photo and an optional note. Nothing about you or the job is sent. Review every field (sections and their order, maximum pages, photo slot, details in the CV header, date format and page size), then press **Approve template**. Other applications for that country reuse it.

Then:

1. **Generate CV** builds a draft only from your confirmed facts. Each line is marked Added, Removed, Reworded or Moved, compared with your master CV (all your usable confirmed facts), and lists the facts it used.
2. A line the checker can't support is flagged: "This isn't in your fact bank.", "Check this number" or "Check this claim". For each flag, **Edit** the line, **Remove** it, or choose **This is true — save it as a fact**.
3. If you type a new claim into a line, the app asks "Save this as a fact?".
4. **Approve CV** stays off until no line needs a check. Read every word before you approve: the checks help, but they can't catch every embellishment.

**Regenerate** takes an optional note, such as "shorter". It keeps your edits unless you choose **Replace everything, including my edits**. **Versions** lists earlier versions, read-only.

### 4. Cover letter

The letter is optional. **Draft the letter** writes the body paragraphs from the same confirmed facts and angle. The app itself writes the greeting, the opening line, the closing and your name. Deal with any flags as in the CV, then press **Approve letter**.

### 5. Submit

Submit shows a checklist: your name saved, CV approved, letter approved or none, files exported, and the page count against the template's maximum.

1. Press **Export files**. The app makes PDF and Word files of the approved CV and, if you approved one, the approved letter. Download each with its button. The files are kept in your data folder and never sent anywhere.
2. Submit the application yourself on the employer's site. **Open the posting** opens the link you saved with the job.
3. Press **I've submitted it — mark Applied**. The app records the versions you sent and moves the application to Applied.

Later edits make new versions. What you sent never changes. Exporting needs your name: add it in Settings > Your details and CV templates. A photo appears only when the template has a photo slot and you've uploaded a photo.

## Interview prep

The **Interview prep** tab generates nothing until you ask.

### Questions

- **Generate questions** builds a first set from the job and the CV you sent, or your latest approved CV if none was sent. The job needs a summary first. Your question bank is reused first, which saves requests.
- **Show more** adds more questions. **Regenerate** makes a new set and asks you to confirm first.
- Filter by **Question type**: Behavioral, Technical, About your CV or Questions to ask them. Each question shows where it came from, such as Generated, Asked in an interview or Reported online.
- **Mark ready** moves a question you've prepared to **Already prepared**.

### Web questions

Under **Research**, **Find web questions** looks for interview questions that people report online for this company and role. It uses AI and web search, with up to five searches per attempt, and shows this month's search count. Each result is labelled "Reported online", with **Open source** and the date the source gives. If the search provider's terms don't allow keeping results, they are shown now only and can't be marked ready.

Web questions run only when you press the button. Generating questions keeps working when web search is blocked by a budget.

### Question bank

Questions you generate, questions you were asked (from your debriefs) and questions found online that may be kept all go into your question bank. **Search bank** finds older questions; without a search, the list shows up to 20 of the newest. Questions tied to another company stay with that company.

### Practice

Press **Practice** on a question, type **Your answer** and press **Get feedback**. The feedback covers **Answers the question**, **Structure**, **Evidence**, **Length** and **Matches the CV you sent**. It also lists what your answer says that the CV you sent doesn't, ideas, and suggested wording.

Under "Claims to check", a claim your fact bank doesn't hold reads "This isn't in your fact bank." If it's true, tick **This is true; add it** and press **Add to my facts**. It becomes a draft fact that you check later; it isn't confirmed. **Your tries** keeps every attempt.

### Narrative cards

**Narrative cards** holds short answers for questions that come up in most interviews: Tell me about yourself, Why this role, Why this company, Why you are leaving, Strengths and Weaknesses. They need an angle, chosen in Tailoring.

- **Draft this card** uses one AI request. Each paragraph shows the facts it rests on, and one without support is flagged. For "Why this company", the AI also reads your notes about the company and its latest saved company brief.
- Check any flagged paragraph, then press **Approve card**.
- Tell me about yourself, Why you are leaving, Strengths and Weaknesses can be copied from an approved card in another application with the same angle (**Use this card from another application**). The copy is a new draft, checked again.
- The **Salary** card uses no AI. **Build salary card** makes it from the minimum salary in your job preferences and the pay the posting states.

For a question reused from your bank, **From your earlier answer** shows your best practice answer from another application. **Adapt to this role** rewrites it for this application from your confirmed facts, with the same checks. Approve it with **Approve answer**.

### Company brief

Under **Research**, and on the company's page, **Get a company brief** looks up a few facts about the company on the web, each with a link to the search result it came from. It sends the company's name, website and industry, uses AI and up to five searches, and counts toward your budgets. The brief is written by AI from web results and isn't checked against your facts, so check the source before relying on it. **Refresh the brief** looks again.

### Debrief after an interview

For 14 days after an interview ends, Today offers a debrief. You can also open it from the **Debrief** link on that interview in the application's timeline. A debrief is optional. You can note:

- **Questions you were asked (one per line)**, up to 20. They join your question bank. Tick **Only for this company** to keep them with this company.
- **How it went**: your own notes. They stay on this computer and are never sent to an AI.
- **Experiences you mentioned or wish you had said**: up to three paragraphs. Only list what is true.
- What happens next: Not sure yet, Next round, Offer, Rejected or Something else. For a next round, give its date, time and time zone to add it to the application.

Press **Save debrief**, or **Not needed**. After saving, **Turn into draft facts** sends each experience paragraph to the AI, one request each, and makes draft facts for you to review. When the next step means a new status, the debrief asks, for example, "Move to Interviewing?". The status changes only if you confirm.

## Fact bank

Your Fact bank holds what you know to be true about your work. Every CV, letter and answer the app drafts is built only from facts you confirmed.

It has four tabs: **Library**, **Drafts to review** (with a count), **Capture inbox** and **Coverage**. **Start session** at the top starts an enrichment session.

### Facts and stories

- A **fact** is one statement, such as a skill, a tool, a certification or a result.
- A **story** has a **Situation**, an **Action** and a **Result**.

In the **Library**, press **Add a fact** or **Add a story**. Each one can have a **Role** (or a new one, with **Employer** and **Job title**), **Skills**, **Tags**, a **Start date** and an **End date** (a year, a month or a full date), and a **Privacy** choice. Find facts with **Search facts**, and filter by role, skill and status: Confirmed, Draft, Archived or Discarded.

Every edit makes a new version. **History** shows the versions and what changed. **Used in** lists the documents that used a fact, and says when the version sent differs from the current one. **Archive** keeps a fact but leaves it out of new CVs and letters; **Restore** brings it back.

Gentle hints, such as "Add a result?", suggest what would make a fact stronger. **Answer in a session** turns a hint into a session question.

### Drafts to review

Drafts come from your CV import, sessions, practice answers and debriefs. A draft is used in a CV or a letter only after you confirm it. Check each one against its source, then:

- **Confirm** it, or **Edit** it and **Save and confirm**;
- **Merge with…** an existing fact that says the same thing;
- **Discard** it. **Undo** is available until you leave the page.

**Confirm all from this import** confirms the drafts from one CV import at once. Drafts marked "Needs a check" (something in them wasn't found in your CV) are left for you to read one by one.

### Importing your CV

In **Drafts to review**, under **Import your CV**, choose a PDF, Word or plain-text file of up to 10 MB, or paste the text, then press **Import CV**.

- Your CV's text, with any contact details in it, is sent to your AI provider so it can list what the CV states.
- A scanned CV needs its text pasted in: the app doesn't read text from images.
- A CV with more than 60,000 characters of text is refused, never cut.
- The file stays in your data folder until you remove it from **Uploaded CVs**. Removing it keeps the facts already made from it. A failed import keeps its upload, so you can try again.

### Capture inbox

Jot down something you did, in a line: **New note**, then **Add note**. When you have time, **Turn into a fact**, **Expand in a session**, or **Delete** it (with **Undo**).

### Coverage

**Coverage** lists the requirements your saved and active jobs ask for, grouped by similar wording, with the biggest gap first. Each row shows how many jobs ask for it and how strong your evidence is:

- **Strong**: two or more confirmed stories;
- **Thin**: facts, or one story;
- **None**.

**Start session** starts a session about that requirement. **Strengthen**, on a Thin row, lists the facts behind it, each with **Refine with AI**. Grouping reads English wording only, so the same requirement in different words can show as two rows.

### Refine with AI

Open a fact and press **Refine with AI**. The AI asks what would make the fact stronger, such as the result, the scale or a number, and may suggest clearer wording. It never adds details you didn't give. You can answer up to three times.

The suggestion shows as a word diff against your fact's current wording. A detail the checker can't find in your facts reads "This isn't in your fact bank.": tick **This is true; add it**, or take it out of the wording. Nothing changes until you press **Accept**, which saves a new version. **Edit it myself** and **Discard** end the conversation. Facts marked "Never use in applications", and archived facts, can't be refined.

### Duplicates and merging

In **Drafts to review**, **Find possible duplicates** compares each draft with your confirmed facts, word by word, and lists the ones that say nearly the same thing. Nothing is sent to the AI. **Compare and merge** shows both wordings; choose the wording to keep, or write your own, and press **Merge**. **Not a duplicate** keeps them apart. A draft's **Merge with…** lets you search for the fact to merge it into. While a merge can be taken back, **Undo merge** is offered.

### Privacy options

Each fact has a **Privacy** choice:

- **Normal**: the app may use it in your CVs and letters and send it to your AI provider.
- **Never use in applications**: kept for your own reference. It is never put in a CV or a letter and never sent to the AI.
- **Generalize names**: the AI is told to describe names in general terms, such as "a large retail bank" instead of a client's name. The fact itself is still sent to the AI.

## Enrichment sessions

A session is a short run of questions, one at a time, that helps you remember your experience and turn it into draft facts.

### Starting a session

You can start one from:

- Today's **Suggested session**;
- **Start session** in the Fact bank or the command palette;
- a row in **Coverage**;
- a gap check: **Answer a few questions about these gaps**;
- an interview in an application's timeline: **Talk through this interview in a session**;
- a rejection: **Start a session about this role's requirements**;
- a capture note: **Expand in a session**;
- a hint on a fact: **Answer in a session**.

On **Start a session**, choose a **Focus** (**Most useful**, or a fact, role, skill, job or note) and a **Length**: 3 questions (about 5 minutes) or 5 questions (about 8 minutes). Only one session can be open at a time; **Continue your session** returns to it. Sessions need an AI provider. Each question and each answer is an AI request and counts toward your usage budget.

### Answering

Each question comes with "Why this question:". Answer in your own words, up to 4,000 characters, and press **Submit answer**. You can also choose **Skip**, **I don't remember** (it comes back later, asked another way), **Ask me later** (next session, in a week, in a month or on a date you pick) or **End session**.

The app checks each question before you see it. A question that asks about a number, employer or skill your facts don't state, that opens with a yes/no word, or that ends with a tag such as "right?" is replaced by a plain open question. This check reads English only.

Each answer becomes a draft fact or story. A detail your answer doesn't state is marked "This isn't in your answer", and your original answer is kept beside the draft. When the session ends, **Confirm**, **Edit** or **Discard** each draft, or **Leave in Drafts to review**. Nothing becomes a confirmed fact until you confirm it.

Settings > Sessions sets how often Today suggests a session.

## Companies

**Companies** lists every company your jobs came from, 50 to a page, sortable by **Name**, **Open roles**, **To apply** and **Last activity**. A company is added when a job summary names it. Names are matched ignoring case, punctuation and endings such as "Inc" or "Ltd".

A company's page shows:

- its website, industry, size and locations; **Edit company** changes them and your notes;
- **Jobs at this company**, each with its state;
- **Contacts**, with **Add a contact**;
- **History**: your applications here. With two or more applications in the last 30 days, a note says so. It never blocks you;
- **Company brief** (see [Interview prep](#company-brief)).

Renaming a company changes how it is matched: a job added later under the old name creates a new company.

## Settings

Settings has ten areas, listed beside the page on a wide screen and above it on a phone.

- **Providers and AI.** How you connect to Claude, with the same choices as setup step 2: **Save**, **Test connection**, and removing a saved token or key. **Model per tier** sets the model for each tier: Fast is for routine tasks such as summaries, Thorough for tailoring and feedback. **Tier per task** moves a task to the other tier. **Testing tools** show only while the practice provider is in use. **Re-run the setup guide** goes through the setup steps again; nothing is reset.
- **Usage budgets.** See [Usage budgets](#usage-budgets).
- **Job preferences.** What you set in setup step 5, read-only, with a link to change it in the setup guide. Below it, your **Removal rules**.
- **Lists and statuses.** Board statuses: rename, move up or down within their phase, or add one (its phase can't change later). Built-in statuses can't be deleted, and a custom status can be deleted only while no application is or was in it. Then the reminder rules; the lists of industries, seniority levels and employment types, each value with other spellings; skills' other spellings; and the fixed list of removal reasons, with the preference each one speaks about.
- **Prompts.** Each AI task's prompt, with its default version and whether you edited it. You can change the wording of a prompt's system and user text. Your version is kept on this computer and used until you press **Reset to default**. A poorly worded prompt can make results worse. The app always adds its own notice that outside text is untrusted, and its honesty rule; you can't remove them.
- **Sessions.** Open **Change session settings** to choose how often Today suggests a session (Never, Weekly, Twice a week or After each application), the default length, the default for **Ask me later**, and **Pause all suggestions**.
- **Your details and CV templates.** Your **Name** (needed to export a CV or letter), email, phone, location and up to three links. A photo: JPEG or PNG, up to 5 MB, 100 to 4000 pixels on each side; hidden data, such as where it was taken, is removed. These go in your CV's header and at the end of your letter, stay on this computer and are never sent to the AI. Below them, your CV templates by country, each approved or a draft.
- **Appearance.** **Theme**: System, Light or Dark. **Density**: Comfortable or Compact.
- **Data and privacy.** Where your data folder is, **Export**, **Import** and **Delete all data…**. See [Your data](#your-data).
- **Keyboard shortcuts.** **Use single-key shortcuts**, on or off.

## Background tasks and notifications

AI work runs in the background, so pages never freeze while it runs. **Tasks** in the top bar counts the running and queued tasks. Open it to see each task's status (Queued, Running, Done, Failed, Cancelled or Interrupted), its progress and its buttons:

- **Cancel** stops a queued or running task. A running task that is cancelled discards its partial output.
- **Retry** runs a failed, cancelled or interrupted task again.

If the app stops while a task runs, the task shows as Interrupted when the app starts again, and runs again only when you press **Retry**. Queued tasks run after a restart. When an AI answer comes back unusable, the app tries once more by itself. "Background tasks at once" in Settings > Usage budgets sets how many tasks run together. Screen readers are told when a task finishes.

**Notifications** lists due reminders and links to tasks that need attention. Opening it marks the reminders shown as seen. Nothing is sent outside the app.

## Usage budgets

Budgets are optional. A budget is a number of requests per month for one service: **Claude subscription**, **Claude API key** or **Web search**, and **Fake provider** while the practice provider is offered. A service without a budget has no limit.

In Settings > Usage budgets, each budget has:

- **Monthly allowance (requests)**;
- **First warning at (requests)** and **Second warning at (requests)**;
- **Blocking point (requests)**, at most the allowance: the app stops using that service there until the reset day;
- **Reset day of the month**: days 29 to 31 move to the last day of shorter months;
- optional **Fast tier cap** and **Thorough tier cap**.

Press **Save budget**, or **Remove budget**.

Each warning shows once per period, in a dialog that says what the count will reach, with **Continue**, **Not now** and **Change limits**. At the blocking point, the action's button is disabled, with the reason and the reset date. When a tier cap blocks a Thorough task, you can choose **Run once on Fast**; the app never switches by itself.

**Usage so far** shows each service's count against its allowance, its reset date, any calls still unfinished, the count made on this computer and the one the provider reports, and a breakdown by task and tier, with token counts. Failed and unfinished calls count too. Limits count requests only. Claude's own plan limits are shown for information and never block anything. A connection test counts as one request and is never blocked.

## The demo

Start the demo with `uv run jobpipeline --demo`. It opens straight into the fictional workspace of Alex Meadow (Fictional), with jobs in every inbox state, companies, applications, interviews and a small fact bank. Dates are moved to the day the demo is filled, so the data looks current.

- The demo has its own data folder and its own keychain service. It never reads or writes your real data folder or setup file, and it ignores credentials in environment variables.
- AI features use the practice provider, "Fake (testing only)". It needs no account and calls no AI service. It doesn't read what you give it: it returns fixed fictional text, labelled as practice data (for example "Practice data (fake provider)").
- Settings > Providers and AI > **Testing tools** > **Start practice task** runs a few steps on the practice provider. It uses none of your data and counts only against the practice provider's usage.
- Two Claude options are listed too. If you choose one and save a token or key, the demo calls Claude.
- To reset the demo, use Settings > Data and privacy > **Delete all data…** inside it. The demo is emptied and its setup guide opens. The next `uv run jobpipeline --demo` start fills it again. First check that the page says "Demo workspace": with `--data-dir` or `JOBPIPELINE_DATA_DIR` set, `--demo` opens that folder instead.

With `--data-dir` or `JOBPIPELINE_DATA_DIR`, `--demo` uses that folder instead: nothing is filled in, and the practice provider is offered only when `JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1` is set.

## Your data

### Where it lives

Everything you add is in your data folder. Settings > Data and privacy shows where it is. Inside it:

- `app.db`: the database;
- `documents`: exported CVs and letters;
- `imports`: uploaded CVs;
- `assets`: your photo;
- `prompts`: your prompt changes;
- `backups`: copies of the database, made before an update changes it;
- `exports`: your export files;
- `logs`: the app's logs.

Your tokens and keys are not in the data folder. They are in your system keychain, or in environment variables if you set them that way.

### Backing up

Settings > Data and privacy > **Export** saves one file with everything in the workspace: your data, documents, photo, CV uploads and prompt changes. Backups, logs, earlier exports and saved keys are not included. Tick **Include provider usage snapshots** to add the saved usage counts; they hold no keys or text.

The file is saved in the `exports` folder. Press **Download** and keep a copy somewhere else. The file isn't encrypted, so keep it private.

### Restoring

A restore goes only into an empty workspace. Nothing is merged.

1. Start the app where there is no workspace yet: on a new computer, or after deleting all data. The setup guide opens at step 1.
2. Under **Restore from an export**, enter a new or empty data folder, choose the export file and press **Restore from this file**.
3. Your data and files come back as they were, and you carry on from where that copy left off.

Settings > Data and privacy > **Import** does the same while the open workspace has no content. An export from an older version is brought up to date; one from a newer version is refused. An export can be up to 200 MB.

Keys are never exported. After a restore, the app may show **Needs attention** with **Fix the AI provider**: open that step and save your sign-in again.

### Deleting all data

1. Export a copy first if you want to keep your work, and download it: exports in the data folder are deleted too.
2. Settings > Data and privacy > **Delete all data…**.
3. Type `delete` in lowercase, then press **Delete all data**.

This removes everything inside the app's folders: your data, documents, imports, assets, prompts, backups, exports and logs. It also removes the keys the app saved in your keychain, and the setup file. Then the setup guide starts again.

- Saved keys are shared by every workspace on this computer; the demo has its own. If the setup file points at another data folder, the keys and the setup file are kept.
- A key you set as an environment variable can't be removed by the app. Remove it from your environment yourself.
- With `JOBPIPELINE_CONFIG_DIR` pointing at a folder that has no setup file, the app can't see your usual setup file, so it treats the saved keys as this workspace's and removes them.
- Files the app didn't create are left in place, and the dialog lists them.
- Deleted files are not securely overwritten, so backups such as Time Machine may keep copies.

## Privacy and honesty

### What stays on your computer

- The app runs on your computer and listens only on `127.0.0.1`, so other devices can't open it.
- Your data stays in your data folder. Your tokens and keys stay in your system keychain and never appear in the data folder, logs or exports.
- The name, contact details and photo you save in Settings are never sent to the AI.
- A debrief's "How it went" is never sent to the AI.
- Facts marked "Never use in applications" are never sent to the AI or put in a document.

### What is sent, and when

The app sends text to your AI provider only for work you start, such as adding a job or pressing an AI button, and only the text that work needs. For example:

- adding a job sends the job text to summarize it, then the job, your confirmed facts and your job preferences to assess your fit;
- importing a CV sends the CV's text;
- tailoring sends the job and your confirmed facts;
- drafting a CV template sends only the country and your choices;
- generating interview questions sends the job summary, the requirements you haven't covered and the CV you sent;
- practice sends your answer when you ask for feedback; adapting an earlier answer sends that answer as you wrote it;
- a "Why this company" card also sends your notes about the company and its latest saved brief;
- web questions send the company, the role's title and the industry, and a company brief sends the company's name, website and industry, to the AI and the search. Neither sends your facts or personal details;
- grouping removal notes sends up to 100 notes with their job IDs.

Fetching a job link reads only that page. Each AI request and web search counts toward your usage budgets.

### How the app keeps drafts honest

- Nothing the AI writes about you becomes a fact, a CV, a letter or an answer until you confirm or approve it.
- CVs, letters, narrative cards and adapted answers are built only from facts you confirmed. The AI must cite the facts behind each line or paragraph it writes, and one it can't support is flagged for your check.
- A checker flags numbers, employers and skills your facts don't state. You edit the claim out, or confirm it's true.
- The checks are approximate and read English only. They can miss an embellishment, such as "led" where your fact says "helped". Read every word before you approve.
- The app never submits an application. You submit each one yourself, then mark it Applied.

## Accessibility

The app aims at WCAG 2.2 AA. Automated checks (axe) run on every page in light and dark themes, at desktop and phone widths, and manual checks with VoiceOver and Orca are part of each release's checklist.

- **Keyboard.** Everything works with the keyboard. **Skip to main content** is the first link on every page. On the Board, **Move to…** on each card moves it without dragging. Esc closes a drawer or dialog, and focus goes back to where you were.
- **Screen readers.** Results, saved changes and finished background tasks are announced, for example "Removed. Undo available." in the Job inbox. Progress lines such as "Exporting…" appear only while their work runs.
- **Shortcuts.** You can turn single-key shortcuts off in Settings > Keyboard shortcuts (see [Getting around](#getting-around)). Ctrl+K (Cmd+K on a Mac) and Esc always work.
- **Display.** Settings > Appearance has **Theme** (System, Light or Dark) and **Density** (Comfortable or Compact). The app follows your system's reduce-motion setting. Pages work from a 360 px wide phone screen and at 200% zoom.
- **Known limits.** The command palette lists its results in one list per kind, so some screen readers may not speak the group headings. On a small phone, the **Delete all data** dialog's buttons start below the fold, so you scroll inside the dialog to reach them.

If something doesn't work with your browser, screen reader or zoom level, open an issue in the project's repository and say which ones you used.
