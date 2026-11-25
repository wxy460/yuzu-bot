(() => {
  const pageSize = 12;
  const allSections = "全部";
  const normalize = value => value.normalize("NFKC").toLowerCase().replaceAll("/", " ").trim();
  const commands = registry.map(([name, original, description, usage, aliases, section, examples, topic]) => {
    const searchText = normalize([name, original, topic, section, description, usage, ...aliases,
      ...examples.flat(), section === "舞萌" ? "maimai maimaidx" : ""].join(" "));
    return {name, original, topic, description, usage, aliases, section, examples, searchText};
  });
  const sections = [...new Set(commands.map(command => command.section))];
  const originalCategories = new Set(commands.map(command => command.original));
  const search = document.querySelector("#search");
  const filters = document.querySelector(".filters");
  const sectionSelect = document.querySelector("#section-select");
  const topic = document.querySelector("#topic");
  const results = document.querySelector("#command-root");
  const count = document.querySelector("#command-count");
  const empty = document.querySelector("#empty");
  const pages = document.querySelector("#command-pages");
  const previous = document.querySelector("#previous-page");
  const next = document.querySelector("#next-page");
  const pageStatus = document.querySelector("#page-status");
  let active = allSections;
  let page = 1;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function sampleRow(command, note, primary) {
    const row = element("div", "", primary ? "main-sample" : "extra-sample");
    const line = element("div", "", "sample-line");
    line.append(element("code", command, "command"));
    const copy = element("button", "复制", "copy");
    copy.type = "button";
    copy.setAttribute("aria-label", "复制 " + command);
    copy.addEventListener("click", () => copyValue(command, "已复制 " + command));
    line.append(copy);
    row.append(line);
    if (note) row.append(element("p", note, "example"));
    return row;
  }

  function commandCard(command, expand) {
    const card = element("article", "", "card");
    card.dataset.command = command.name;
    const [[first, note], ...more] = command.examples;
    if (normalize(first).split(/\s/)[0] !== normalize(command.name)) {
      card.append(element("p", "/" + command.name, "command-key"));
    }
    card.append(sampleRow(first, "", true), element("p", command.description, "desc"));
    if (note && note !== command.description) card.append(element("p", note, "example"));
    if (more.length || command.usage !== first || command.aliases.length) {
      const details = element("details", "", "command-details");
      const summary = more.length ? "更多用法（" + more.length + "）" : "指令用法";
      details.append(element("summary", summary));
      if (command.usage !== first) details.append(element("p", "用法 · " + command.usage, "example"));
      if (command.aliases.length) {
        details.append(element("p", "别名 · " + command.aliases.join("、"), "example"));
      }
      for (const [sample, hint] of more) details.append(sampleRow(sample, hint, false));
      if (expand) details.open = true;
      card.append(details);
    }
    return card;
  }

  function searchTerms() {
    const input = normalize(search.value);
    const menu = input.replace(/^(?:help|帮助|菜单)\s*/, "").replace(/^文字(?:菜单)?(?:\s+|$)/, "");
    const parts = menu.replace(/^maimai\s*dx\b/, "maimaidx").split(/[\s>·]+/);
    const [group, subtopic] = parts;
    const section = ["maimai", "maimaidx", "舞萌dx"].includes(group) ? "舞萌" : group;
    if (sections.includes(section) && parts.length === 1) return {section, words: []};
    if (sections.includes(section) && parts.length === 2 &&
        commands.some(command => command.section === section && command.topic === subtopic)) {
      return {section, topic: subtopic, words: []};
    }
    if (menu !== input && originalCategories.has(menu)) return {original: menu, words: []};
    if (menu && (menu !== input || search.value.trim().startsWith("/"))) {
      const exact = commands.find(command => [command.name, ...command.aliases].includes(menu));
      if (exact) return {exact: exact.name, words: [], expand: true};
    }
    const text = menu !== input && menu ? menu : input;
    return {words: text.split(/\s+/).filter(Boolean)};
  }

  function render() {
    const query = searchTerms();
    const words = query.words;
    const matches = commands.filter(command =>
      (active === allSections || command.section === active) &&
      (!topic.value || command.topic === topic.value) &&
      (!query.section || command.section === query.section) &&
      (!query.topic || command.topic === query.topic) &&
      (!query.original || command.original === query.original) &&
      (!query.exact || command.name === query.exact) &&
      words.every(word => command.searchText.includes(word))
    );
    const totalPages = Math.ceil(matches.length / pageSize);
    const current = matches.slice((page - 1) * pageSize, page * pageSize);
    results.replaceChildren();
    for (const section of sections) {
      const entries = current.filter(command => command.section === section);
      if (!entries.length) continue;
      const group = element("section", "", "group");
      group.dataset.category = section;
      const heading = element("div", "", "section-head");
      heading.append(element("h2", section));
      const sectionCount = matches.filter(command => command.section === section).length;
      heading.append(element("span", sectionCount + " 个指令", "count"));
      const grid = element("div", "", "grid");
      for (const command of entries) grid.append(commandCard(command, words.length || query.expand));
      group.append(heading, grid);
      results.append(group);
    }
    count.textContent = "共 " + matches.length + " 个指令";
    empty.classList.toggle("show", !matches.length);
    pages.hidden = totalPages <= 1;
    pageStatus.textContent = matches.length ? "第 " + page + " / " + totalPages + " 页" : "";
    previous.disabled = page <= 1;
    next.disabled = page >= totalPages;
  }

  function updateSection(section) {
    active = section;
    page = 1;
    sectionSelect.value = section;
    for (const button of filters.children) {
      button.classList.toggle("active", button.dataset.filter === active);
      button.setAttribute("aria-pressed", String(button.dataset.filter === active));
    }
    topic.replaceChildren(new Option("全部分类", ""));
    const entries = commands.filter(command => active === allSections || command.section === active);
    for (const name of new Set(entries.map(command => command.topic).filter(Boolean))) {
      topic.add(new Option(name, name));
    }
    topic.parentElement.hidden = topic.options.length === 1;
    render();
  }

  for (const section of [allSections, ...sections]) {
    const button = element("button", section, "filter");
    button.type = "button";
    button.dataset.filter = section;
    button.addEventListener("click", () => updateSection(section));
    filters.append(button);
    sectionSelect.add(new Option(section, section));
  }
  sectionSelect.addEventListener("change", () => updateSection(sectionSelect.value));
  search.addEventListener("input", () => { page = 1; render(); });
  topic.addEventListener("change", () => { page = 1; render(); });
  document.querySelector("#reset-search").addEventListener("click", () => {
    search.value = "";
    updateSection(allSections);
    search.focus();
  });
  function turnPage(delta) {
    page += delta;
    render();
    const controlsHeight = document.querySelector(".controls").getBoundingClientRect().height;
    const target = results.getBoundingClientRect().top + window.scrollY - controlsHeight;
    window.scrollTo({top: target, behavior: "instant"});
    const firstTitle = results.querySelector("h2");
    firstTitle.tabIndex = -1;
    firstTitle.focus({preventScroll: true});
  }
  previous.addEventListener("click", () => turnPage(-1));
  next.addEventListener("click", () => turnPage(1));
  updateSection(allSections);
})();
