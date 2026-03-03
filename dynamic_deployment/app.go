package main

import (
	"os/exec"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/bubbles/list"
	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/lipgloss"
)

//
// STYLES
//

type Styles struct {
	Header     lipgloss.Style
	InputField lipgloss.Style
	Footer     lipgloss.Style
	Panel      lipgloss.Style
	Selected   lipgloss.Style
	Unselected lipgloss.Style
}

func DefaultStyles() *Styles {
	glow := lipgloss.Color("99")

	return &Styles{
		Header:     lipgloss.NewStyle().Bold(true).Foreground(glow).Padding(0, 1),
		InputField: lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(glow).Padding(1).Width(60),
		Footer:     lipgloss.NewStyle().Foreground(lipgloss.Color("241")).Padding(0, 1),
		Panel:      lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(glow).Padding(1).Margin(1),
		Selected:   lipgloss.NewStyle().Foreground(glow).Bold(true),
		Unselected: lipgloss.NewStyle().Foreground(lipgloss.Color("252")),
	}
}

//
// LIST ITEM
//

type item struct {
	title string
}

func (i item) Title() string       { return i.title }
func (i item) Description() string { return "" }
func (i item) FilterValue() string { return i.title }

//
// MODEL
//

type model struct {
	appList     list.Model
	problemList list.Model
	selectedApp string
	answer      textinput.Model
	mode        string
	width       int
	height      int
	styles      *Styles
	status      string
}

//
// FETCH PROBLEMS FROM PYTHON REGISTRY
//

func fetchProblems() []list.Item {
	cmd := exec.Command("uv", "run", "deploy.py", "list-problems")
	out, err := cmd.Output()
	if err != nil {
		return []list.Item{item{"Error loading problems"}}
	}

	lines := strings.Split(string(out), "\n")
	var items []list.Item
	for _, line := range lines {
		if line != "" {
			items = append(items, item{line})
		}
	}
	return items
}

//
// NEW MODEL
//

func New() *model {
	styles := DefaultStyles()

	delegate := list.NewDefaultDelegate()
	delegate.ShowDescription = false
	delegate.SetHeight(1)
	delegate.SetSpacing(0)

	appNames := fetchApps()

	var appItems []list.Item
	for _, name := range appNames {
		appItems = append(appItems, item{title: name})
	}
	

	problemItems := fetchProblems()

	appList := list.New(appItems, delegate, 0, 0)
	appList.SetShowStatusBar(false)
	appList.SetFilteringEnabled(false)
	appList.SetShowHelp(false)

	problemList := list.New(problemItems, delegate, 0, 0)
	problemList.SetShowStatusBar(false)
	problemList.SetFilteringEnabled(false)
	problemList.SetShowHelp(false)

	answer := textinput.New()
	answer.Placeholder = "Type problem ID..."
	answer.Focus()

	return &model{
		appList:     appList,
		problemList: problemList,
		answer:      answer,
		mode:        "select",
		styles:      styles,
		status:      "",
	}
}

func (m model) Init() tea.Cmd {
	return nil
}

//
// UPDATE
//

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {

	switch msg := msg.(type) {

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.appList.SetSize(msg.Width-6, msg.Height-8)
		m.problemList.SetSize(msg.Width-6, msg.Height-8)
		return m, nil

	case tea.KeyMsg:

		switch m.mode {

		case "select":

			switch msg.String() {

			case "enter":
				app := m.appList.SelectedItem().(item).Title()
				m.selectedApp = app
				m.status = "Deployed application: " + app
				go deployApp(app)
				return m, nil

			case "p":
				m.mode = "problem_select"
				return m, nil

			case "q", "ctrl+c":
				return m, tea.Quit
			}

			var cmd tea.Cmd
			m.appList, cmd = m.appList.Update(msg)
			return m, cmd

		case "problem_select":

			switch msg.String() {

			case "enter":
				problem := m.problemList.SelectedItem().(item).Title()
				m.status = "Ran problem: " + problem
				go runProblem(problem)
				return m, nil
			case "r":
        		problem := m.problemList.SelectedItem().(item).Title()
				m.status = "Recovered problem: " + problem
        		go recoverProblem(problem)
        		return m, nil
    

			case "i":
				m.answer.SetValue("")
				m.mode = "problem_input"
				return m, nil

			case "esc":
				m.mode = "select"
				return m, nil
			}

			var cmd tea.Cmd
			m.problemList, cmd = m.problemList.Update(msg)
			return m, cmd

		case "problem_input":

			switch msg.String() {

			case "enter":
				problem := m.answer.Value()
				m.status = "Ran problem: " + problem
				go runProblem(problem)
				m.answer.SetValue("")
				m.mode = "problem_select"
				return m, nil

			case "esc":
				m.mode = "problem_select"
				return m, nil
			}

			var cmd tea.Cmd
			m.answer, cmd = m.answer.Update(msg)
			return m, cmd
		}
	}

	return m, nil
}

//
// VIEW
//

func (m model) View() string {

	if m.width == 0 {
		return "Loading..."
	}

	statusLine := m.styles.Footer.Render(m.status)

	switch m.mode {

	case "select":

		header := m.styles.Header.Render("DEPLOY APPLICATION (press p for problems)")
		panel := m.styles.Panel.Width(m.width - 6).Render(m.appList.View())

		return lipgloss.JoinVertical(
			lipgloss.Center,
			header,
			panel,
			m.styles.Footer.Render("Enter Deploy  •  p Problems  •  q Quit"),
			statusLine,
		)

	case "problem_select":

		header := m.styles.Header.Render("SELECT PROBLEM (press i to type manually)")
		panel := m.styles.Panel.Width(m.width - 6).Render(m.problemList.View())

		return lipgloss.JoinVertical(
			lipgloss.Center,
			header,
			panel,
			m.styles.Footer.Render("Enter Run  •  i Manual  •  Esc Back"),
			statusLine,
		)

	case "problem_input":

		header := m.styles.Header.Render("TYPE PROBLEM ID")

		return lipgloss.JoinVertical(
			lipgloss.Center,
			header,
			"",
			m.styles.InputField.Render(m.answer.View()),
			"",
			m.styles.Footer.Render("Enter Run  •  Esc Back"),
			statusLine,
		)
	}

	return ""
}

//
// BACKEND CALLS
//
func fetchApps() []string {
	cmd := exec.Command("uv", "run", "deploy.py", "list-apps")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return []string{"Error loading applications"}
	}

	lines := strings.Split(string(out), "\n")
	var results []string
	for _, l := range lines {
		if l != "" {
			results = append(results, l)
		}
	}
	return results
}
func deployApp(app string) {
	cmd := exec.Command("uv", "run", "deploy.py", "deploy", "--application", app)
	cmd.Run()
}

func runProblem(problem string) {
	cmd := exec.Command("uv", "run", "deploy.py", "run", "--problem", problem)
	cmd.Run()
}
func recoverProblem(problem string) {
    cmd := exec.Command("uv", "run", "deploy.py","recover", "--problem", problem)

    output, err := cmd.CombinedOutput()
    if err != nil {
        fmt.Println("Recovery error:", err)
        return
    }

    fmt.Println(string(output))
}

//
// MAIN
//

func main() {
	m := New()
	p := tea.NewProgram(m, tea.WithAltScreen())
	if err := p.Start(); err != nil {
		panic(err)
	}
}